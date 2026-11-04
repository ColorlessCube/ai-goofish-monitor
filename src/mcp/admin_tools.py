"""Independent settings capabilities: non-secret fields only, stored destinations only."""
from __future__ import annotations

import os

from typing import Literal

from fastapi import HTTPException
from pydantic import Field, model_validator

from src.infrastructure.config.env_manager import env_manager
from src.mcp.outcomes import MutationOutcomeError
from src.mcp.result_admin_helpers import Input, dependency, redact, require_revision, revision, tool



def get_settings_store():
    """Override this provider on app.dependency_overrides in isolated consumers."""
    return env_manager


class SettingsWrite(Input):
    expected_revision: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def actual_patch(self):
        changes = self.model_dump(exclude_unset=True, exclude={"expected_revision"})
        if not changes or any(value is None for value in changes.values()):
            raise ValueError("nonempty_nonnull_patch_required")
        return self


class AIWrite(SettingsWrite):
    OPENAI_MODEL_NAME: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
    SKIP_AI_ANALYSIS: bool | None = None


def boolean(store, name, default=False):
    return str(store.get_value(name, str(default))).lower() in {"true", "1", "yes", "on"}


def ai_state(store):
    return {"OPENAI_MODEL_NAME": redact(store.get_value("OPENAI_MODEL_NAME", ""), 128),
            "SKIP_AI_ANALYSIS": boolean(store, "SKIP_AI_ANALYSIS"),
            "backend_configured": bool(store.get_value("OPENAI_BASE_URL", "")),
            "credential_configured": bool(store.get_value("OPENAI_API_KEY", ""))}


def revision_values(snapshot, state_builder):
    if state_builder is ai_state:
        fields = ("OPENAI_MODEL_NAME", "SKIP_AI_ANALYSIS", "OPENAI_API_KEY", "OPENAI_BASE_URL", "PROXY_URL")
    elif state_builder is notification_state:
        from src.services.notification_config_service import NOTIFICATION_FIELD_MAP
        fields = NOTIFICATION_FIELD_MAP
    else:
        fields = (*RotationWrite.model_fields.keys(), "ACCOUNT_STATE_DIR", "PROXY_POOL")
    return {name: snapshot.get_value(name) for name in fields if name != "expected_revision"}


def settings_response(snapshot, state_builder):
    return {"settings": state_builder(snapshot), "revision": revision(revision_values(snapshot, state_builder))}


async def get_ai(args, ctx):
    store = await dependency(ctx, get_settings_store)
    return settings_response(store.snapshot(), ai_state)


def persist_patch(args, store, state_builder):
    with store.transaction():
        require_revision(args.expected_revision, revision_values(store.snapshot(), state_builder))
        patch = args.model_dump(exclude_unset=True, exclude={"expected_revision"})
        encoded = {k: str(v).lower() if isinstance(v, bool) else str(v) for k, v in patch.items()}
        if not store.update_values(encoded):
            raise HTTPException(503, "settings_write_failed")
        # Verify the exact settings on disk before reporting success.
        snapshot = store.snapshot()
        if any(snapshot.get_value(k) != v for k, v in encoded.items()):
            raise HTTPException(503, "settings_readback_failed")
        if store is env_manager:
            from src.infrastructure.config.settings import reload_settings
            os.environ.update(encoded)
            reload_settings()
        return settings_response(snapshot, state_builder)


async def update_ai(args, ctx):
    return persist_patch(args, await dependency(ctx, get_settings_store), ai_state)


class NotificationWrite(SettingsWrite):
    PCURL_TO_MOBILE: bool | None = None


class RotationWrite(SettingsWrite):
    ACCOUNT_ROTATION_ENABLED: bool | None = None
    ACCOUNT_ROTATION_MODE: Literal["per_task", "on_failure"] | None = None
    ACCOUNT_ROTATION_RETRY_LIMIT: int | None = Field(default=None, ge=0, le=10)
    ACCOUNT_BLACKLIST_TTL: int | None = Field(default=None, ge=60, le=604800)
    PROXY_ROTATION_ENABLED: bool | None = None
    PROXY_ROTATION_MODE: Literal["per_task", "on_failure"] | None = None
    PROXY_ROTATION_RETRY_LIMIT: int | None = Field(default=None, ge=0, le=10)
    PROXY_BLACKLIST_TTL: int | None = Field(default=None, ge=60, le=604800)


CHANNEL_REQUIREMENTS = {
    "ntfy": ("NTFY_TOPIC_URL",), "bark": ("BARK_URL",),
    "gotify": ("GOTIFY_URL", "GOTIFY_TOKEN"), "wecom": ("WX_BOT_URL",),
    "telegram": ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"), "webhook": ("WEBHOOK_URL",),
}


def notification_state(store):
    return {"PCURL_TO_MOBILE": boolean(store, "PCURL_TO_MOBILE", True),
            "configured_channels": [name for name, fields in CHANNEL_REQUIREMENTS.items()
                                    if all(store.get_value(field, "") for field in fields)]}


def rotation_state(store):
    result = {}
    for prefix in ("ACCOUNT", "PROXY"):
        result[prefix + "_ROTATION_ENABLED"] = boolean(store, prefix + "_ROTATION_ENABLED")
        mode = store.get_value(prefix + "_ROTATION_MODE", "per_task")
        result[prefix + "_ROTATION_MODE"] = mode if mode in {"per_task", "on_failure"} else "unknown"
        for suffix, default in (("_ROTATION_RETRY_LIMIT", 2), ("_BLACKLIST_TTL", 300)):
            value = store.get_value(prefix + suffix, str(default))
            try:
                result[prefix + suffix] = int(value)
            except (TypeError, ValueError):
                result[prefix + suffix] = None
    return result


async def get_notification(args, ctx):
    store = await dependency(ctx, get_settings_store)
    return settings_response(store.snapshot(), notification_state)


async def update_notification(args, ctx):
    return persist_patch(args, await dependency(ctx, get_settings_store), notification_state)


async def get_rotation(args, ctx):
    store = await dependency(ctx, get_settings_store)
    return settings_response(store.snapshot(), rotation_state)


async def update_rotation(args, ctx):
    return persist_patch(args, await dependency(ctx, get_settings_store), rotation_state)


class NotificationTest(Input):
    channel: Literal["ntfy", "bark", "gotify", "wecom", "telegram", "webhook"]


class StoredAIProbe:
    async def run(self, store):
        import asyncio
        from src.api.routes.settings import test_ai_settings
        store = store.snapshot()
        # Only server-stored values enter this internal REST-compatible request.
        payload = {name: store.get_value(name, "") for name in
                   ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL_NAME", "PROXY_URL")}
        return await asyncio.to_thread(lambda: asyncio.run(test_ai_settings(payload)))


class StoredNotificationProbe:
    async def run(self, store, *, channel):
        store = store.snapshot()
        from src.services.notification_config_service import (
            NOTIFICATION_FIELD_MAP, _build_notification_settings_model,
            prepare_notification_test_settings,
        )
        from src.infrastructure.config.settings import DEFAULT_TELEGRAM_API_BASE_URL
        from src.services.notification_service import build_notification_service
        values = {attr: store.get_value(key) or None for key, attr in NOTIFICATION_FIELD_MAP.items()}
        values.update({"telegram_api_base_url": values["telegram_api_base_url"] or DEFAULT_TELEGRAM_API_BASE_URL,
                       "webhook_method": values["webhook_method"] or "POST",
                       "webhook_content_type": values["webhook_content_type"] or "JSON",
                       "pcurl_to_mobile": boolean(store, "PCURL_TO_MOBILE", True)})
        settings = prepare_notification_test_settings({}, _build_notification_settings_model(values), channel=channel)
        # The existing service removes other channels before building clients.
        service = build_notification_service(settings)
        results = await service.send_test_notification()
        result = results.get(channel, {"success": False, "attempted": False, "outcome": "not_attempted"})
        return {key: result[key] for key in ("success", "attempted", "outcome", "retry_allowed") if key in result}


def get_ai_probe():
    return StoredAIProbe()


def get_notification_probe():
    return StoredNotificationProbe()


def probe_result(result, **extra):
    success = result.get("success") is True
    outcome = "accepted" if success else result.get("outcome", "unknown")
    if outcome not in {"accepted", "rejected", "unknown", "not_attempted"}:
        outcome = "unknown"
    value = {"success": success, "attempted": result.get("attempted", True) is True,
             "outcome": outcome, "retry_allowed": False,
             "error_code": None if success else "probe_failed", **extra}
    if not success:
        raise MutationOutcomeError("unknown" if outcome == "unknown" else "failed", value)
    return value


async def probe_ai(args, ctx):
    store = (await dependency(ctx, get_settings_store)).snapshot()
    if not store.get_value("OPENAI_BASE_URL") or not store.get_value("OPENAI_MODEL_NAME"):
        raise MutationOutcomeError("failed", {"success": False, "attempted": False,
            "outcome": "not_attempted", "retry_allowed": False, "error_code": "backend_not_configured"})
    try:
        probe = await dependency(ctx, get_ai_probe)
        result = await probe.run(store)
    except MutationOutcomeError:
        raise
    except Exception:
        result = {"success": False, "attempted": True, "outcome": "unknown"}
    return probe_result(result)


async def probe_notification(args, ctx):
    store = (await dependency(ctx, get_settings_store)).snapshot()
    if args.channel not in notification_state(store)["configured_channels"]:
        raise MutationOutcomeError("failed", {"success": False, "attempted": False,
            "outcome": "not_attempted", "retry_allowed": False,
            "error_code": "channel_not_configured", "channel": args.channel})
    try:
        probe = await dependency(ctx, get_notification_probe)
        result = await probe.run(store, channel=args.channel)
    except MutationOutcomeError:
        raise
    except Exception:
        result = {"success": False, "attempted": True, "outcome": "unknown"}
    return probe_result(result, channel=args.channel, delivery_status="unknown")


def build_tools():
    return [
        tool("test_ai_connection", "Make a bounded connection test using the saved AI backend only; no caller-supplied targets or credentials.", Input, "settings:test", probe_ai, read_only=False),
        tool("test_notification", "Send one real test notification to one already configured channel only; requires send authorization.", NotificationTest, "notifications:send", probe_notification, read_only=False),
        tool("get_notification_settings", "Read notification behavior and configured channel names only, without destinations or credentials.", Input, "settings:read", get_notification),
        tool("update_notification_settings", "Update non-secret notification behavior only; requires revision and readback.", NotificationWrite, "settings:write", update_notification, read_only=False),
        tool("get_rotation_settings", "Read bounded rotation policy fields without account paths or proxy pools.", Input, "settings:read", get_rotation),
        tool("update_rotation_settings", "Update rotation policy only, with revision; cannot modify credential sources or proxy targets.", RotationWrite, "settings:write", update_rotation, read_only=False),
        tool("get_ai_settings", "Read non-secret AI settings and their revision; no keys, URLs or proxies.", Input, "settings:read", get_ai),
        tool("update_ai_settings", "Update model name or analysis enablement only, with revision and readback. Cannot redirect a saved key.", AIWrite, "settings:write", update_ai, read_only=False),
    ]
