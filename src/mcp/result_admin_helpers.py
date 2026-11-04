"""Internal safety helpers for result, diagnostic and settings MCP tools."""
from __future__ import annotations

import hashlib
import inspect
import json
import re

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict
from src.mcp.outcomes import MutationOutcomeError


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


def revision(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), default=str).encode()).hexdigest()


def require_revision(expected: str, value) -> None:
    if expected != revision(value):
        raise HTTPException(409, "revision_conflict")


def redact(value: object, limit: int = 1024) -> str:
    text = str(value or "")
    # Suppress the whole line on credential markers, including spaced/JSON forms.
    if (re.search(r"(?i)(cookie|authorization|bearer|api.?key|token|password|secret|webhook|proxy|sk-[\w-]+|ass_[\w-]+)", text)
            or re.search(r'''(?i)["'](?:value|headers|body|storage|session)["']\s*:''', text)):
        return "[REDACTED]"
    text = re.sub(r"https?://\S+", "[URL REDACTED]", text)
    text = re.sub(r"[\x00-\x1f\x7f]", " ", text)
    return text[:limit]


# Only a fresh timestamped logger record can re-establish trust after a
# truncated source window or an ambiguous multiline credential event.
_LOG_RECORD_START = re.compile(r"^(?:\[ ?)?\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}")


def redact_log_lines(lines: list[str], *, partial_start: bool) -> list[str]:
    """Redact complete events before pagination, preserving source line counts.

    Unframed plain lines remain useful. A structured dump/header and its possible
    continuations are one event until the next logger timestamp. Unknown leading
    fragments fail closed; dropping just the first physical line is not enough.
    """
    output = []
    event = []
    unknown = partial_start
    multiline = partial_start

    def flush():
        if not event:
            return
        sensitive = unknown or redact("\n".join(event), 0) == "[REDACTED]"
        output.extend(["[REDACTED]"] * len(event) if sensitive else
                      [redact(line, 512) for line in event])
        event.clear()

    for line in lines:
        if _LOG_RECORD_START.match(line):
            flush()
            unknown = False
            multiline = True
        elif not multiline:
            flush()
        event.append(line)
        if ("{" in line or "[" in line or redact(line, 0) == "[REDACTED]"):
            multiline = True
    flush()
    return output


async def dependency(ctx, provider):
    factory = ctx["app"].dependency_overrides.get(provider, provider)
    result = factory()
    return await result if inspect.isawaitable(result) else result


def tool(name, description, model, scope, handler, *, read_only=True, destructive=False):
    async def guarded(arguments, ctx):
        if scope not in ctx.get("principal", {}).get("scopes", set()):
            raise HTTPException(403, "insufficient_scope")
        try:
            return await handler(arguments, ctx)
        except MutationOutcomeError:
            raise  # Transport owns safe payloads and durable outcome receipts.
        except HTTPException as exc:
            known = {"revision_conflict", "result_not_found", "result_set_not_found", "too_many_rules",
                     "settings_write_failed", "settings_readback_failed", "task_not_found", "forbidden_log_path"}
            if isinstance(exc.detail, str) and exc.detail in known:
                raise
            raise HTTPException(503, "source_unavailable") from None
        except Exception:
            # Never expose arbitrary source errors, URLs or local filesystem paths.
            raise HTTPException(503, "source_unavailable") from None

    return {"name": name, "description": description, "input_model": model,
            "scope": scope, "read_only": read_only, "destructive": destructive,
            "handler": guarded}
