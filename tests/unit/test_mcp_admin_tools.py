"""Settings tests use temporary .env files; probes never hit a network."""
import asyncio
import importlib
import json

import pytest
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError
from src.infrastructure.config.env_manager import EnvManager


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    import socket
    def denied(*args, **kwargs):
        raise AssertionError("network forbidden in MCP settings unit tests")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


@pytest.fixture
def admin_context(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    manager = EnvManager(str(tmp_path / ".env"))
    manager.update_values({"OPENAI_API_KEY": "fixture-api-secret", "OPENAI_BASE_URL": "https://stored.invalid/key-secret",
        "OPENAI_MODEL_NAME": "fixture-model", "PROXY_URL": "http://user:pass@proxy.invalid",
        "WEBHOOK_URL": "https://stored.invalid/webhook-secret", "PROXY_POOL": "http://secret@proxy.invalid",
        "ACCOUNT_ROTATION_RETRY_LIMIT": "2", "SKIP_AI_ANALYSIS": "false", "PCURL_TO_MOBILE": "true"})
    app = FastAPI()
    return {"app": app, "principal": {"subject": "unit", "scopes": {
        "settings:read", "settings:write", "settings:test", "notifications:send"}}, "manager": manager}


def call(ctx, name, **arguments):
    module = importlib.import_module("src.mcp.admin_tools")
    ctx["app"].dependency_overrides[module.get_settings_store] = lambda: ctx["manager"]
    tools = {t["name"]: t for t in module.build_tools()}
    tool = tools[name]
    return asyncio.run(tool["handler"](tool["input_model"](**arguments), ctx))


def test_ai_allowlisted_read_update_and_stale_write(admin_context):
    before = call(admin_context, "get_ai_settings")
    assert before["settings"]["OPENAI_MODEL_NAME"] == "fixture-model"
    assert "fixture-api-secret" not in json.dumps(before)
    assert "stored.invalid" not in json.dumps(before)
    updated = call(admin_context, "update_ai_settings", expected_revision=before["revision"],
                   OPENAI_MODEL_NAME="new-model", SKIP_AI_ANALYSIS=True)
    assert updated["settings"]["OPENAI_MODEL_NAME"] == "new-model"
    assert call(admin_context, "get_ai_settings") == updated
    assert admin_context["manager"].get_value("OPENAI_API_KEY") == "fixture-api-secret"
    with pytest.raises(HTTPException) as exc:
        call(admin_context, "update_ai_settings", expected_revision=before["revision"], SKIP_AI_ANALYSIS=False)
    assert exc.value.status_code == 409
    for forbidden in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "PROXY_URL", "Cookie"):
        with pytest.raises(ValidationError):
            call(admin_context, "update_ai_settings", expected_revision=updated["revision"], **{forbidden: "do-not-accept"})


@pytest.mark.parametrize("group,field,value,forbidden", [
    ("notification", "PCURL_TO_MOBILE", False, "WEBHOOK_URL"),
    ("rotation", "ACCOUNT_ROTATION_RETRY_LIMIT", 4, "PROXY_POOL"),
])
def test_nonsecret_settings_revision_and_boundaries(admin_context, group, field, value, forbidden):
    before = call(admin_context, f"get_{group}_settings")
    assert "secret" not in json.dumps(before).lower()
    updated = call(admin_context, f"update_{group}_settings", expected_revision=before["revision"], **{field: value})
    assert updated["settings"][field] == value
    assert call(admin_context, f"get_{group}_settings") == updated
    with pytest.raises(HTTPException) as exc:
        call(admin_context, f"update_{group}_settings", expected_revision=before["revision"], **{field: value})
    assert exc.value.status_code == 409
    with pytest.raises(ValidationError):
        call(admin_context, f"update_{group}_settings", expected_revision=updated["revision"], **{forbidden: "https://evil.invalid"})


@pytest.mark.parametrize("kind,tool_name,arguments,scope", [
    ("ai", "test_ai_connection", {}, "settings:test"),
    ("notification", "test_notification", {"channel": "webhook"}, "notifications:send"),
])
def test_probes_use_stored_backend_and_scrub_failures(admin_context, kind, tool_name, arguments, scope):
    module = importlib.import_module("src.mcp.admin_tools")
    calls = []
    class Probe:
        async def run(self, store, **kwargs):
            from src.infrastructure.config.env_manager import EnvSnapshot
            calls.append((isinstance(store, EnvSnapshot), kwargs))
            return {"success": True, "message": "fixture-api-secret", "response": "cookie=fixture-secret"}
    provider = getattr(module, f"get_{kind}_probe", None)
    assert provider is not None, "stored-backend probe provider missing"
    admin_context["app"].dependency_overrides[provider] = lambda: Probe()
    result = call(admin_context, tool_name, **arguments)
    assert result["success"] is True
    assert "secret" not in json.dumps(result)
    assert calls == [(True, arguments)]
    with pytest.raises(ValidationError):
        call(admin_context, tool_name, **arguments, OPENAI_BASE_URL="https://evil.invalid")
    admin_context["principal"]["scopes"].remove(scope)
    with pytest.raises(HTTPException) as exc:
        call(admin_context, tool_name, **arguments)
    assert exc.value.status_code == 403 and len(calls) == 1
    admin_context["principal"]["scopes"].add(scope)
    class Failure:
        async def run(self, store, **kwargs):
            raise RuntimeError("fixture-api-secret https://secret.invalid")
    admin_context["app"].dependency_overrides[provider] = lambda: Failure()
    from src.mcp.outcomes import MutationOutcomeError
    with pytest.raises(MutationOutcomeError) as failure:
        call(admin_context, tool_name, **arguments)
    result = failure.value.payload
    assert failure.value.status == "unknown" and result["attempted"] is True
    assert result["success"] is False and result["error_code"] == "probe_failed"
    assert "secret" not in json.dumps(result)


def test_rotation_accepts_only_existing_application_modes(admin_context):
    before = call(admin_context, "get_rotation_settings")
    result = call(admin_context, "update_rotation_settings", expected_revision=before["revision"],
                  ACCOUNT_ROTATION_MODE="on_failure", PROXY_ROTATION_MODE="on_failure")
    assert result["settings"]["ACCOUNT_ROTATION_MODE"] == "on_failure"
    with pytest.raises(ValidationError):
        call(admin_context, "update_rotation_settings", expected_revision=result["revision"], ACCOUNT_ROTATION_MODE="per_request")


def test_default_ai_adapter_only_passes_stored_settings(admin_context, monkeypatch):
    from src.mcp.admin_tools import StoredAIProbe
    from src.api.routes import settings
    seen = []
    async def fake_request(payload):
        seen.append(payload)
        return {"success": True, "response": "untrusted fixture response"}
    monkeypatch.setattr(settings, "test_ai_settings", fake_request)
    result = asyncio.run(StoredAIProbe().run(admin_context["manager"]))
    assert result["success"] is True
    assert seen == [{key: admin_context["manager"].get_value(key, "") for key in
                     ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL_NAME", "PROXY_URL")}]


def test_default_notification_adapter_disables_other_channels(admin_context, monkeypatch):
    from src.mcp.admin_tools import StoredNotificationProbe
    from src.services import notification_service
    admin_context["manager"].set_value("NTFY_TOPIC_URL", "https://stored.invalid/other-channel")
    selected = []
    class FakeService:
        async def send_test_notification(self):
            return {"webhook": {"success": True, "message": "fixture-secret"}}
    def factory(settings):
        selected.append(settings)
        assert settings.webhook_url == "https://stored.invalid/webhook-secret"
        assert settings.ntfy_topic_url is None and settings.telegram_bot_token is None
        return FakeService()
    monkeypatch.setattr(notification_service, "build_notification_service", factory)
    result = asyncio.run(StoredNotificationProbe().run(admin_context["manager"], channel="webhook"))
    assert result == {"success": True} and len(selected) == 1


def test_catalog_is_complete_scoped_and_forbids_extra_inputs():
    expected = {
        "results:read": {"list_result_sets", "query_results", "get_result", "get_price_insights", "get_blacklist_rules", "export_results", "preview_result_cleanup"},
        "results:write": {"set_result_status", "update_blacklist_rules"},
        "results:delete": {"delete_result_set"},
        "system:read": {"get_health", "get_dashboard_summary", "get_system_status", "get_task_logs", "get_failure_guard_status"},
        "accounts:read": {"get_account_source_status", "list_account_metadata"},
        "settings:read": {"get_ai_settings", "get_notification_settings", "get_rotation_settings"},
        "settings:write": {"update_ai_settings", "update_notification_settings", "update_rotation_settings"},
        "settings:test": {"test_ai_connection"}, "notifications:send": {"test_notification"},
    }
    tools = [tool for module in ("result_tools", "diagnostic_tools", "admin_tools")
             for tool in importlib.import_module("src.mcp." + module).build_tools()]
    assert len(tools) == sum(len(names) for names in expected.values())
    assert {tool["scope"] for tool in tools} == set(expected)
    for scope, names in expected.items():
        assert {tool["name"] for tool in tools if tool["scope"] == scope} == names
    for tool in tools:
        schema = tool["input_model"].model_json_schema()
        assert schema["additionalProperties"] is False
        assert tool["read_only"] == tool["scope"].endswith(":read")
        assert tool["destructive"] == (tool["name"] == "delete_result_set")
        assert not {"OPENAI_API_KEY", "OPENAI_BASE_URL", "WEBHOOK_URL", "Cookie", "PROXY_URL", "PROXY_POOL"}.intersection(schema.get("properties", {}))
