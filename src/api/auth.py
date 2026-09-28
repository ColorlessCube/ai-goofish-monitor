"""Authentication and authorization for the HTTP API.

The browser receives a short-lived signed, HttpOnly session after a successful
password login. Automation uses a separate read-only bearer token. The token
never grants write access and is deliberately restricted to an explicit
allowlist of safe endpoints.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from typing import Final

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

SESSION_COOKIE: Final = "goofish_session"
SESSION_TTL_SECONDS: Final = 8 * 60 * 60
READ_ONLY_PREFIXES: Final = (
    "/api/readonly",  # Small stable endpoint used by the local MCP adapter.
    "/api/dashboard/summary",
    "/api/tasks",
    "/api/results",
    "/api/logs",
    "/api/settings/status",
)

_audit_logger = logging.getLogger("ai_goofish_monitor.access_audit")


def _session_secret() -> bytes:
    secret = os.environ.get("GOOFISH_SESSION_SECRET", "")
    if not secret:
        raise RuntimeError("GOOFISH_SESSION_SECRET is required for authenticated API access")
    return secret.encode("utf-8")


def issue_admin_session(subject: str) -> str:
    """Create a signed browser-only admin session without storing a password."""
    payload = json.dumps(
        {"sub": subject, "scope": "admin", "exp": int(time.time()) + SESSION_TTL_SECONDS},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).rstrip(b"=")
    signature = hmac.new(_session_secret(), encoded, hashlib.sha256).digest()
    return encoded.decode("ascii") + "." + base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")


def verify_admin_session(token: str | None) -> str | None:
    if not token or "." not in token:
        return None
    encoded_text, signature_text = token.split(".", 1)
    encoded = encoded_text.encode("ascii")
    expected = hmac.new(_session_secret(), encoded, hashlib.sha256).digest()
    try:
        signature = base64.urlsafe_b64decode(signature_text + "=" * (-len(signature_text) % 4))
        payload = json.loads(base64.urlsafe_b64decode(encoded + b"=" * (-len(encoded) % 4)))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not hmac.compare_digest(signature, expected):
        return None
    if payload.get("scope") != "admin" or not isinstance(payload.get("sub"), str):
        return None
    if not isinstance(payload.get("exp"), int) or payload["exp"] < int(time.time()):
        return None
    return payload["sub"]


def _read_token_subject(token: str | None) -> str | None:
    configured = os.environ.get("GOOFISH_MCP_READ_TOKEN", "")
    if not configured or not token or not hmac.compare_digest(token, configured):
        return None
    return os.environ.get("GOOFISH_MCP_READ_SUBJECT", "hermes-mcp-readonly")


def _bearer_token(request: Request) -> str | None:
    scheme, _, value = request.headers.get("Authorization", "").partition(" ")
    return value if scheme.lower() == "bearer" and value else None


def _is_read_only_path(path: str) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") for prefix in READ_ONLY_PREFIXES)


def _audit(principal: str, request: Request, outcome: str) -> None:
    _audit_logger.info(
        "access_audit principal=%s method=%s path=%s outcome=%s",
        principal,
        request.method,
        request.url.path,
        outcome,
    )


class ApiAuthorizationMiddleware(BaseHTTPMiddleware):
    """Deny every /api request except a signed admin session or scoped token."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if not path.startswith("/api/"):
            return await call_next(request)

        try:
            session_subject = verify_admin_session(request.cookies.get(SESSION_COOKIE))
        except RuntimeError:
            session_subject = None
        if session_subject:
            request.state.principal = session_subject
            request.state.scope = "admin"
            _audit(session_subject, request, "allowed")
            return await call_next(request)

        subject = _read_token_subject(_bearer_token(request))
        if subject:
            if request.method not in {"GET", "HEAD"} or not _is_read_only_path(path):
                _audit(subject, request, "forbidden")
                return JSONResponse(status_code=403, content={"detail": "read-only token cannot access this operation"})
            request.state.principal = subject
            request.state.scope = "read"
            _audit(subject, request, "allowed")
            return await call_next(request)

        _audit("anonymous", request, "unauthenticated")
        return JSONResponse(status_code=401, content={"detail": "authentication required"})
