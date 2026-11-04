"""Native HTTP MCP protocol and authorization, no real external calls."""
import importlib.util
import json

import pytest
from pydantic import BaseModel, ConfigDict

from fastapi import FastAPI
from fastapi.testclient import TestClient


READ_TOKEN = "readonly-fixture-" + "r" * 40
WRITE_TOKEN = "writer-fixture-" + "w" * 40


def client_for(monkeypatch, tmp_path):
    monkeypatch.setenv("GOOFISH_MCP_ENABLED", "true")
    monkeypatch.setenv("GOOFISH_MCP_READ_TOKEN", READ_TOKEN)
    monkeypatch.setenv("GOOFISH_MCP_WRITE_TOKEN", WRITE_TOKEN)
    monkeypatch.setenv("GOOFISH_MCP_WRITE_SCOPES", "tasks:read,tasks:write")
    monkeypatch.setenv("GOOFISH_MCP_ALLOWED_HOSTS", "testserver")
    monkeypatch.setenv("GOOFISH_MCP_STATE_DIR", str(tmp_path / "mcp-state"))
    assert importlib.util.find_spec("src.mcp.remote") is not None, "native MCP route is missing"
    from src.mcp.remote import router
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def rpc(client, method, params=None, token=READ_TOKEN, **headers):
    return client.post("/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}, headers={"Authorization": "Bearer " + token, "Accept": "application/json, text/event-stream", **headers})


def test_native_initialize_negotiates_stateless_json_transport(monkeypatch, tmp_path):
    client = client_for(monkeypatch, tmp_path)
    response = rpc(client, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}})
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["serverInfo"]["name"] == "goofish"
    assert "tools" in result["capabilities"]
    assert "mcp-session-id" not in response.headers


def test_native_endpoint_is_disabled_by_default(monkeypatch, tmp_path):
    client = client_for(monkeypatch, tmp_path)
    monkeypatch.delenv("GOOFISH_MCP_ENABLED")
    assert rpc(client, "initialize").status_code == 404


def test_native_endpoint_requires_valid_bearer(monkeypatch, tmp_path):
    client = client_for(monkeypatch, tmp_path)
    assert rpc(client, "initialize", token="wrong").status_code == 401
    assert client.post("/api/mcp", json={}).status_code == 401


def test_native_endpoint_rejects_untrusted_host_and_origin(monkeypatch, tmp_path):
    client = client_for(monkeypatch, tmp_path)
    assert rpc(client, "initialize", Host="attacker.example").status_code == 403
    assert rpc(client, "initialize", Origin="https://attacker.example").status_code == 403


def test_native_endpoint_does_not_upgrade_a_read_token(monkeypatch, tmp_path):
    client = client_for(monkeypatch, tmp_path)
    monkeypatch.setenv("GOOFISH_MCP_WRITE_TOKEN", READ_TOKEN)
    assert rpc(client, "initialize").status_code == 503


def test_native_endpoint_requires_json_accept_and_bounds_body(monkeypatch, tmp_path):
    client = client_for(monkeypatch, tmp_path)
    assert rpc(client, "initialize", Accept="text/html").status_code == 406
    r = client.post("/api/mcp", content='{"padding":"' + 'x' * 150000 + '"}', headers={"Authorization": "Bearer " + READ_TOKEN, "Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
    assert r.status_code == 413


class EchoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str = "ok"


def install_tools(monkeypatch):
    from src.mcp import remote
    async def echo(args, ctx):
        return {"value": args.value, "subject": ctx["principal"]["subject"]}
    tools = [{"name": "read_fixture", "description": "Read fixture", "input_model": EchoArgs, "scope": "tasks:read", "read_only": True, "destructive": False, "handler": echo}, {"name": "write_fixture", "description": "Write fixture", "input_model": EchoArgs, "scope": "tasks:write", "read_only": False, "destructive": False, "handler": echo}]
    monkeypatch.setattr(remote, "get_tools", lambda: tools, raising=False)
    return tools


def test_scopes_filter_discovery_and_are_enforced_on_call(monkeypatch, tmp_path):
    client = client_for(monkeypatch, tmp_path)
    install_tools(monkeypatch)
    listed = rpc(client, "tools/list").json()["result"]
    assert [t["name"] for t in listed.get("tools", [])] == ["read_fixture"]
    denied = rpc(client, "tools/call", {"name": "write_fixture", "arguments": {}}).json()
    assert denied["error"]["code"] == -32602
    allowed = rpc(client, "tools/list", token=WRITE_TOKEN).json()["result"]["tools"]
    write = next(t for t in allowed if t["name"] == "write_fixture")
    assert "request_id" in write["inputSchema"]["required"]
    assert write["annotations"]["readOnlyHint"] is False


def test_read_tool_returns_structured_and_text_content(monkeypatch, tmp_path):
    client = client_for(monkeypatch, tmp_path)
    install_tools(monkeypatch)
    result = rpc(client, "tools/call", {"name": "read_fixture", "arguments": {"value": "hello"}}).json()["result"]
    assert result.get("isError") is False
    assert result["structuredContent"]["value"] == "hello"
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
    invalid = rpc(client, "tools/call", {"name": "read_fixture", "arguments": {"unexpected": "secret"}}).json()
    assert invalid["error"]["code"] == -32602
    assert "secret" not in json.dumps(invalid)


@pytest.mark.parametrize("body", [[], {"method": "tools/list", "id": 1}, {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "write_fixture"}}, {"jsonrpc": "2.0", "id": True, "method": "ping"}])
def test_malformed_rpc_never_runs_a_tool(monkeypatch, tmp_path, body):
    client = client_for(monkeypatch, tmp_path)
    install_tools(monkeypatch)
    response = client.post("/api/mcp", json=body, headers={"Authorization": "Bearer " + WRITE_TOKEN, "Accept": "application/json, text/event-stream"})
    assert response.status_code == 400


def test_notifications_and_unknown_method(monkeypatch, tmp_path):
    client = client_for(monkeypatch, tmp_path)
    r = client.post("/api/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers={"Authorization": "Bearer " + READ_TOKEN, "Accept": "application/json, text/event-stream"})
    assert r.status_code == 202 and not r.content
    assert rpc(client, "does/not/exist").json()["error"]["code"] == -32601


def test_write_receipt_survives_new_client_and_rejects_changed_reuse(monkeypatch, tmp_path):
    client = client_for(monkeypatch, tmp_path)
    tools = install_tools(monkeypatch)
    effects = []
    async def mutate(args, ctx):
        effects.append(args.value)
        return {"value": args.value}
    tools[1]["handler"] = mutate
    params = {"name": "write_fixture", "arguments": {"value": "first", "request_id": "2b8ec18a-0bf2-48c0-99df-150dfd77a220"}}
    first = rpc(client, "tools/call", params, token=WRITE_TOKEN).json()
    assert first.get("result", {}).get("isError") is False
    second = rpc(client_for(monkeypatch, tmp_path), "tools/call", params, token=WRITE_TOKEN).json()
    assert first["result"] == second["result"]
    assert len(effects) == 1
    receipt = first["result"]["structuredContent"]["_receipt"]
    assert receipt["status"] == "succeeded" and receipt["invocation_id"]
    params["arguments"]["value"] = "changed"
    conflict = rpc(client, "tools/call", params, token=WRITE_TOKEN).json()
    assert conflict["result"]["isError"] is True
    assert len(effects) == 1


def test_failed_mutation_is_not_replayed_and_does_not_leak_exception(monkeypatch, tmp_path):
    client = client_for(monkeypatch, tmp_path)
    tools = install_tools(monkeypatch)
    effects = []
    async def partially_applied(args, ctx):
        effects.append(1)
        raise RuntimeError("private-cookie-SHOULD-NOT-LEAK")
    tools[1]["handler"] = partially_applied
    params = {"name": "write_fixture", "arguments": {"request_id": "b70e1161-0d75-4d96-8f94-611ef11b2ecb"}}
    for _ in range(2):
        reply = rpc(client, "tools/call", params, token=WRITE_TOKEN).json()
        assert reply.get("result", {}).get("isError") is True
        assert reply["result"]["structuredContent"]["_receipt"]["status"] == "unknown"
        assert "SHOULD-NOT-LEAK" not in json.dumps(reply)
    assert effects == [1]


def test_tool_output_redacts_credentials_and_limits_size(monkeypatch, tmp_path):
    client = client_for(monkeypatch, tmp_path)
    tools = install_tools(monkeypatch)
    async def sensitive(args, ctx):
        return {"api_key": "secret-key-value", "nested": {"password": "secret-password-value"}, "log": "Authorization: Bearer hidden-bearer-value\nCookie: hidden-cookie-value\n" + READ_TOKEN}
    tools[0]["handler"] = sensitive
    response = rpc(client, "tools/call", {"name": "read_fixture"}).json()
    assert response.get("result", {}).get("isError") is False
    text = json.dumps(response)
    for forbidden in ("secret-key-value", "secret-password-value", "hidden-bearer-value", "hidden-cookie-value", READ_TOKEN):
        assert forbidden not in text
    async def large(args, ctx):
        return {"value": "x" * 300000}
    tools[0]["handler"] = large
    response = rpc(client, "tools/call", {"name": "read_fixture"})
    assert len(response.content) < 270000
    assert response.json()["result"]["isError"] is True


def test_real_app_routes_mcp_without_granting_rest_writes(monkeypatch, tmp_path):
    client_for(monkeypatch, tmp_path)
    from src.app import app
    client = TestClient(app)
    assert rpc(client, "initialize").status_code == 200
    assert client.post("/api/tasks", json={}, headers={"Authorization": "Bearer " + READ_TOKEN}).status_code == 403
    assert client.post("/api/tasks", json={}, headers={"Authorization": "Bearer " + WRITE_TOKEN}).status_code == 401
    assert client.post("/api/mcp/anything", json={}, headers={"Authorization": "Bearer " + WRITE_TOKEN}).status_code == 401
    monkeypatch.setenv("GOOFISH_SESSION_SECRET", "fixture-session-key" * 3)
    from src.api.auth import issue_admin_session, SESSION_COOKIE
    client.cookies.set(SESSION_COOKIE, issue_admin_session("fixture-admin"))
    assert client.post("/api/mcp", json={}).status_code == 401


def test_receipt_query_is_subject_isolated(monkeypatch, tmp_path):
    client = client_for(monkeypatch, tmp_path)
    tools = install_tools(monkeypatch)
    from src.mcp import remote
    assert hasattr(remote, "receipt_tools"), "receipt-query tool is missing"
    tools.extend(remote.receipt_tools())
    monkeypatch.setenv("GOOFISH_MCP_WRITE_SCOPES", "tasks:read,tasks:write,system:read")
    request_id = "16922438-bc5c-4262-97b7-22cbcb6cf39f"
    assert rpc(client, "tools/call", {"name": "write_fixture", "arguments": {"request_id": request_id}}, token=WRITE_TOKEN).json()["result"]["isError"] is False
    query = {"name": "get_invocation", "arguments": {"request_id": request_id}}
    owned = rpc(client, "tools/call", query, token=WRITE_TOKEN).json()["result"]["structuredContent"]
    assert owned["status"] == "succeeded"
    other = rpc(client, "tools/call", query).json()["result"]
    assert other["isError"] is True


def test_interrupted_claim_is_not_reclaimed(monkeypatch, tmp_path):
    client = client_for(monkeypatch, tmp_path)
    tools = install_tools(monkeypatch)
    from src.mcp import journal
    request_id = "e5353a24-38b7-4c7b-9b3b-72e64020fbe0"
    action, _ = journal.claim("writer:ass-mcp", request_id, "write_fixture", {})
    assert action == "new"
    effects = []
    async def mutate(args, ctx):
        effects.append(1)
        return {}
    tools[1]["handler"] = mutate
    response = rpc(client, "tools/call", {"name": "write_fixture", "arguments": {"request_id": request_id}}, token=WRITE_TOKEN).json()["result"]
    assert response["isError"] is True
    assert response["structuredContent"]["_receipt"]["status"] == "running_or_unknown"
    assert effects == []


def test_protocol_rejects_non_utf8_duplicate_keys_and_missing_sse_accept(monkeypatch, tmp_path):
    client = client_for(monkeypatch, tmp_path)
    headers = {"Authorization": "Bearer " + READ_TOKEN, "Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    for body in ['{"jsonrpc":"2.0","id":1,"method":"ping"}'.encode("utf-16"), b'{"jsonrpc":"2.0","id":1,"method":"ping","method":"initialize"}']:
        assert client.post("/api/mcp", content=body, headers=headers).status_code == 400
    assert rpc(client, "ping", Accept="application/json").status_code == 406
    assert rpc(client, "ping", **{"MCP-Protocol-Version": "2099-01-01"}).status_code == 400
    assert client.get("/api/mcp", headers=headers).status_code == 405
