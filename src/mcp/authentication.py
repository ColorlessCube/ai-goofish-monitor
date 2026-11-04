"""Separate native MCP machine authority; never impersonate browser admins."""
import hmac
import os
import re
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

# ASCII bearer-token grammar, validated before constant-time string comparison.
BEARER_TOKEN = re.compile(r"[A-Za-z0-9._~+/-]+=*", re.ASCII)

READ_SCOPES = frozenset({"tasks:read", "results:read", "system:read"})
ALL_SCOPES = frozenset({
    *READ_SCOPES, "tasks:write", "tasks:run", "tasks:delete",
    "prompts:read", "prompts:write", "prompts:delete",
    "results:write", "results:delete", "settings:read", "settings:write",
    "settings:test", "notifications:send", "accounts:read",
})


def authenticate(request: Request) -> dict:
    if os.environ.get("GOOFISH_MCP_ENABLED", "false").lower() != "true":
        raise HTTPException(404, "mcp_disabled")
    host_values = request.headers.getlist("host")
    allowed = {x.strip().lower() for x in os.environ.get(
        "GOOFISH_MCP_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]"
    ).split(",") if x.strip()}
    try:
        parsed = urlsplit("http://" + (host_values[0] if len(host_values) == 1 else ""))
        host = parsed.hostname or ""
        valid_host = not parsed.username and not parsed.password and not parsed.path and not parsed.query and not parsed.fragment
        port = parsed.port
        del port
    except ValueError:
        valid_host, host = False, ""
    if not valid_host or (host.lower() not in allowed and ("[" + host.lower() + "]") not in allowed):
        raise HTTPException(403, "mcp_host_denied")
    origins = request.headers.getlist("origin")
    approved_origins = {x.strip() for x in os.environ.get("GOOFISH_MCP_ALLOWED_ORIGINS", "").split(",") if x.strip()}
    if origins and (len(origins) != 1 or origins[0] not in approved_origins):
        raise HTTPException(403, "mcp_origin_denied")
    read = os.environ.get("GOOFISH_MCP_READ_TOKEN", "")
    write = os.environ.get("GOOFISH_MCP_WRITE_TOKEN", "")
    if any(value and (len(value) < 32 or not BEARER_TOKEN.fullmatch(value)) for value in (read, write)):
        raise HTTPException(503, "mcp_credentials_invalid")
    if read and write and hmac.compare_digest(read, write):
        raise HTTPException(503, "mcp_credentials_invalid")
    values = request.headers.getlist("authorization")
    parts = values[0].split(" ") if len(values) == 1 and len(values[0]) <= 1024 else []
    token = parts[1] if len(parts) == 2 and parts[0].lower() == "bearer" else ""
    if not BEARER_TOKEN.fullmatch(token):
        raise HTTPException(401, "mcp_auth_required", headers={"WWW-Authenticate": "Bearer"})
    if token and read and hmac.compare_digest(token, read):
        return {"subject": "readonly:" + os.environ.get("GOOFISH_MCP_READ_SUBJECT", "hermes-mcp-readonly"), "scopes": READ_SCOPES}
    if token and write and hmac.compare_digest(token, write):
        scopes = frozenset(x.strip() for x in os.environ.get("GOOFISH_MCP_WRITE_SCOPES", "").split(",") if x.strip())
        if not scopes or not scopes.issubset(ALL_SCOPES):
            raise HTTPException(503, "mcp_scopes_invalid")
        return {"subject": "writer:" + os.environ.get("GOOFISH_MCP_WRITE_SUBJECT", "ass-mcp"), "scopes": scopes}
    raise HTTPException(401, "mcp_auth_required", headers={"WWW-Authenticate": "Bearer"})
