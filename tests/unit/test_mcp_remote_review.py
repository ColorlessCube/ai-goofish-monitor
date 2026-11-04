"""Independent transport review: synthetic handlers, no external connections."""
import json
import socket
import sqlite3
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict

from src.api.auth import ApiAuthorizationMiddleware, issue_admin_session
from src.mcp import journal, remote

READ = "review-read-" + "r" * 40
WRITE = "review-write-" + "w" * 40


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str = "ok"


@pytest.fixture
def setup(monkeypatch, tmp_path):
    def denied(*args, **kwargs):
        raise AssertionError("external network forbidden in review")
    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    for k, v in {
        "GOOFISH_MCP_ENABLED": "true",
        "GOOFISH_MCP_READ_TOKEN": READ,
        "GOOFISH_MCP_WRITE_TOKEN": WRITE,
        "GOOFISH_MCP_WRITE_SCOPES": "tasks:read,tasks:write,system:read",
        "GOOFISH_MCP_ALLOWED_HOSTS": "testserver",
        "GOOFISH_MCP_ALLOWED_ORIGINS": "",
        "GOOFISH_MCP_STATE_DIR": str(tmp_path / "state"),
        "GOOFISH_MCP_WRITE_SUBJECT": "independent-review",
        "GOOFISH_MCP_READ_SUBJECT": "independent-review",
        "GOOFISH_SESSION_SECRET": "review-only-session-secret-" * 3,
    }.items():
        monkeypatch.setenv(k, v)
    effects = []
    async def echo(args, ctx):
        effects.append(ctx["principal"]["subject"])
        return {"value": args.value}
    tools = [
        {"name": "review_read", "description": "Synthetic read", "input_model": Args,
         "scope": "tasks:read", "read_only": True, "destructive": False, "handler": echo},
        {"name": "review_write", "description": "Synthetic write", "input_model": Args,
         "scope": "tasks:write", "read_only": False, "destructive": False, "handler": echo},
        *remote.receipt_tools(),
    ]
    monkeypatch.setattr(remote, "get_tools", lambda: tools)
    app = FastAPI()
    app.add_middleware(ApiAuthorizationMiddleware)
    app.include_router(remote.router)
    client = TestClient(app, raise_server_exceptions=False)
    return client, tools, effects, tmp_path


def rpc(client, name, arguments=None, token=READ):
    return client.post("/api/mcp", json={"jsonrpc": "2.0", "id": "review", "method": "tools/call", "params": {"name": name, "arguments": arguments or {}}}, headers={"Authorization": "Bearer " + token, "Accept": "application/json, text/event-stream"})


@pytest.mark.parametrize("kind", ["json_api_key", "standalone_bearer", "token_field", "secret_dynamic_key", "json_cookie", "json_basic_auth", "json_token", "passwd_assignment", "nested_json", "escaped_json", "unicode_json_key", "prefixed_json", "multiline_json", "folded_header", "standalone_basic", "token_assignment", "dynamic_credential_key", "quoted_multiline_password", "escaped_fragment", "credential_object_assignment"])
def test_sensitive_outputs_never_leak(setup, monkeypatch, kind):
    client, tools, effects, tmp_path = setup
    marker = "review-sensitive-903x"
    payloads = {
        "json_api_key": {"log": json.dumps({"api_key": marker})},
        "standalone_bearer": {"log": "upstream rejected Bearer " + marker},
        "token_field": {"token": marker},
        "secret_dynamic_key": {marker: "ordinary value"},
        "json_cookie": {"log": json.dumps({"Cookie": "sid=" + marker})},
        "json_basic_auth": {"log": json.dumps({"Authorization": "Basic " + marker})},
        "json_token": {"log": json.dumps({"token": marker})},
        "passwd_assignment": {"log": "passwd=" + marker},
        "nested_json": {"log": json.dumps({"entry": json.dumps({"Cookie": "sid=" + marker})})},
        "escaped_json": {"log": json.dumps(json.dumps({"token": marker}))},
        "unicode_json_key": {"log": '{"to\\u006ben": "' + marker + '"}'},
        "prefixed_json": {"log": 'upstream response: ' + json.dumps({"headers": {"Cookie": "sid=" + marker}, "code": 401})},
        "multiline_json": {"log": json.dumps({"Authorization": "Basic " + marker, "Cookie": "sid=" + marker}, indent=2)},
        "folded_header": {"log": "Cookie: sid=ordinary;\n\tother=" + marker + "\nDone"},
        "standalone_basic": {"log": "upstream rejected Basic " + marker},
        "token_assignment": {"log": "token=" + marker},
        "dynamic_credential_key": {"log": json.dumps({"upstream_session_token": marker})},
        "quoted_multiline_password": {"log": 'passwd="first line\n' + marker + '"'},
        "escaped_fragment": {"log": "escaped response: " + json.dumps(json.dumps({"Cookie": "sid=" + marker, "token": marker}))[1:-1]},
        "credential_object_assignment": {"log": "Cookie: " + json.dumps({"sid": marker})},
    }
    if kind == "secret_dynamic_key":
        monkeypatch.setenv("REVIEW_API_KEY", marker)
    async def payload(args, ctx):
        effects.append(ctx["principal"]["subject"])
        return payloads[kind]
    tools[0]["handler"] = tools[1]["handler"] = payload
    read = rpc(client, "review_read")
    rid = str(uuid4())
    write = rpc(client, "review_write", {"request_id": rid}, token=WRITE)
    for response in (read, write):
        assert response.status_code == 200 and response.json()["result"]["isError"] is False
    replay = rpc(client, "review_write", {"request_id": rid}, token=WRITE)
    assert replay.json()["result"] == write.json()["result"]
    receipt = rpc(client, "get_invocation", {"request_id": rid}, token=WRITE)
    assert receipt.json()["result"]["structuredContent"]["result"] == write.json()["result"]
    with sqlite3.connect(tmp_path / "state" / "invocations.sqlite3") as db:
        rows = db.execute("SELECT response_json FROM invocations WHERE request_id=?", (rid,)).fetchall()
    assert len(rows) == 1
    assert json.loads(rows[0][0]) == write.json()["result"]
    assert effects == ["readonly:independent-review", "writer:independent-review"]
    outputs = {"read": read.text, "write": write.text, "replay": replay.text, "receipt_api": receipt.text, "sqlite": rows[0][0]}
    leaks = [surface for surface, raw in outputs.items() if marker in raw]
    assert not leaks, {"case": kind, "exposed_surfaces": leaks}


def test_non_ascii_bearer_is_auth_rejection_not_500(setup):
    client, _, effects, _ = setup
    response = client.post("/api/mcp", content=b'{"jsonrpc":"2.0","id":1,"method":"ping"}', headers=[(b"Authorization", b"Bearer invalid-\xff"), (b"Accept", b"application/json, text/event-stream"), (b"Content-Type", b"application/json")])
    assert effects == []
    assert response.status_code == 401, (response.status_code, response.text)


@pytest.mark.parametrize("payload", [{"value": "\ud800"}, {"value": {"unsupported-set"}}])
def test_unserializable_write_output_stays_unknown_and_nonreplayable(setup, payload):
    client, tools, effects, _ = setup
    async def mutate(args, ctx):
        effects.append(1)
        return payload
    tools[1]["handler"] = mutate
    rid = str(uuid4())
    first = rpc(client, "review_write", {"request_id": rid}, token=WRITE)
    assert first.status_code == 200 and first.json()["error"]["code"] == -32603
    second = rpc(client, "review_write", {"request_id": rid}, token=WRITE)
    assert second.json()["result"]["structuredContent"]["_receipt"]["status"] == "running_or_unknown"
    assert second.json()["result"]["structuredContent"]["_receipt"]["retry_allowed"] is False
    own = rpc(client, "get_invocation", {"request_id": rid}, token=WRITE).json()["result"]
    assert own["structuredContent"]["status"] == "running_or_unknown"
    other = rpc(client, "get_invocation", {"request_id": rid}).json()["result"]
    assert other["isError"] is True
    assert effects == [1]


def test_write_http_500_retains_unknown_and_never_replays(setup):
    client, tools, effects, _ = setup
    async def mutate(args, ctx):
        effects.append(1)
        raise HTTPException(500, "review-private-exception")
    tools[1]["handler"] = mutate
    rid = str(uuid4())
    first = rpc(client, "review_write", {"request_id": rid}, token=WRITE)
    second = rpc(client, "review_write", {"request_id": rid}, token=WRITE)
    assert first.json()["result"] == second.json()["result"]
    assert first.json()["result"]["structuredContent"]["_receipt"]["status"] == "unknown"
    assert "review-private-exception" not in first.text
    assert effects == [1]


@pytest.mark.parametrize("body", [
    {"jsonrpc": "2.0", "id": "\ud800", "method": "ping"},
    {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "review_write", "arguments": {"request_id": "6a79c48f-5c43-48d5-9073-77d004049f45", "value": "\udfff"}}},
    {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {"\ud800": "x"}},
])
def test_invalid_unicode_rejected_before_any_effect(setup, body):
    client, _, effects, _ = setup
    response = client.post("/api/mcp", content=json.dumps(body).encode(), headers={"Authorization": "Bearer " + WRITE, "Accept": "application/json, text/event-stream", "Content-Type": "application/json"})
    assert response.status_code == 400 and response.json()["error"]["code"] == -32600
    assert response.json()["id"] is None
    assert effects == []


def test_cookie_and_read_token_do_not_elevate_write_scope(setup, monkeypatch):
    client, _, effects, _ = setup
    client.cookies.set("goofish_session", issue_admin_session("review-admin"))
    rid = str(uuid4())
    assert client.post("/api/mcp", json={}).status_code == 401
    assert rpc(client, "review_write", {"request_id": rid}).json()["error"]["code"] == -32602
    monkeypatch.setenv("GOOFISH_MCP_WRITE_SCOPES", "tasks:read")
    assert rpc(client, "review_write", {"request_id": rid}, token=WRITE).json()["error"]["code"] == -32602
    assert effects == []


def test_bad_uuid_never_claims_or_executes(setup):
    client, _, effects, tmp_path = setup
    for rid in ("6A79C48F-5C43-48D5-9073-77D004049F45", "{6a79c48f-5c43-48d5-9073-77d004049f45}", "6a79c48f5c4348d5907377d004049f45"):
        response = rpc(client, "review_write", {"request_id": rid}, token=WRITE)
        assert response.json()["error"]["code"] == -32602
    assert effects == []
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize("bad", [b"invalid-\xff", b"invalid-\t", b"invalid-\r", b"invalid-\n", b"invalid-\x00", b"invalid-\x7f", b"invalid-:", b"invalid-\"", b"invalid-=inside"])
def test_invalid_bearer_syntax_is_rejected_before_compare(setup, monkeypatch, bad):
    from src.mcp import authentication
    client, _, effects, _ = setup
    compared = []
    original = authentication.hmac.compare_digest

    def track_compare(left, right):
        compared.append((left, right))
        return original(left, right)

    monkeypatch.setattr(authentication.hmac, "compare_digest", track_compare)
    response = client.post("/api/mcp", content=b'{"jsonrpc":"2.0","id":1,"method":"ping"}', headers=[(b"Authorization", b"Bearer " + bad), (b"Accept", b"application/json, text/event-stream"), (b"Content-Type", b"application/json")])
    assert response.status_code == 401
    assert all(right != bad.decode("latin-1") and left != bad.decode("latin-1") for left, right in compared)
    assert effects == []


@pytest.mark.parametrize("credential", ["GOOFISH_MCP_READ_TOKEN", "GOOFISH_MCP_WRITE_TOKEN"])
@pytest.mark.parametrize("suffix", ["é", " ", "\t", "\n", ":", '"', "=inside"])
def test_invalid_configured_credential_fails_closed(setup, monkeypatch, credential, suffix):
    client, _, effects, _ = setup
    monkeypatch.setenv(credential, "fixture-config-" + "x" * 40 + suffix)
    assert rpc(client, "review_read").status_code == 503
    assert effects == []


def test_ascii_bearer_alphabet_still_works(setup, monkeypatch):
    client, _, effects, _ = setup
    token = "fixture-._~+/-" + "x" * 40 + "=="
    monkeypatch.setenv("GOOFISH_MCP_READ_TOKEN", token)
    assert rpc(client, "review_read", token=token).json()["result"]["isError"] is False
    assert effects == ["readonly:independent-review"]


def test_structured_string_redaction_preserves_usage_and_is_idempotent():
    from src.mcp.redaction import redact
    marker = "structured-private-fixture-903"
    safe = {"usage": {"prompt_tokens": 3, "token": 17},
            "token": {"prompt": 3, "completion": 14},
            "prompt_config": {"max_tokens": 1024, "token_limit": 2048, "tokenizer": "fixture-encoding"},
            "text": "adjust token limits, token=512"}
    for encode in (lambda x: json.dumps(x), lambda x: json.dumps(x, indent=2), lambda x: json.dumps(json.dumps(x))):
        untouched = encode(safe)
        assert redact(untouched) == untouched
        unsafe = encode({**safe, "Cookie": "sid=" + marker, "Authorization": "Basic " + marker, "log": "token=" + marker})
        redacted = redact(unsafe)
        assert marker not in redacted
        assert redact(redacted) == redacted
        decoded = json.loads(redacted)
        if isinstance(decoded, str):
            decoded = json.loads(decoded)
        assert {key: decoded[key] for key in safe} == safe
