"""Outer credentials and whole environment secrets at every MCP boundary."""
import json
import sqlite3
from uuid import uuid4

import pytest

from tests.unit.test_mcp_remote_review import WRITE, rpc, setup  # noqa: F401


@pytest.mark.parametrize("case", [
    "quoted_password_json", "cookie_embedded_json", "quoted_password_json_suffix",
    "environment_json_dynamic_key", "environment_json_value",
    "credential_array_assignment", "credential_object_assignment",
    "multiline_cookie_embedded_json", "multiline_cookie_quoted_value",
    "nested_outer_credential", "escaped_outer_credential",
    "environment_json_nested_value", "environment_json_nested_key",
    "environment_json_escaped_value", "environment_json_escaped_fragment",
])
def test_adjacent_sensitive_text(setup, monkeypatch, case):
    client, tools, effects, tmp_path = setup
    marker = "fresh-final-private-marker-903"
    payloads = {
        "quoted_password_json": {"log": "passwd='" + json.dumps({"value": marker}) + "'"},
        "cookie_embedded_json": {"log": "Cookie: sid=" + json.dumps({"value": marker}) + "; other=1"},
        "quoted_password_json_suffix": {"log": 'passwd="before [1,2] ' + marker + '"'},
        "credential_array_assignment": {"log": "passwd=" + json.dumps([{"value": marker}, marker]) + " safe=ordinary"},
        "credential_object_assignment": {"log": "upstreamPassword=" + json.dumps({"value": [marker]}) + " safe=ordinary"},
        "multiline_cookie_embedded_json": {"log": 'Cookie: sid={\n"value": "' + marker + '"\n}; other=1\nSafe event'},
        "multiline_cookie_quoted_value": {"log": 'Cookie: sid="first line\n' + marker + '"\nSafe event'},
        "nested_outer_credential": {"log": json.dumps({"entry": 'passwd="before [1,2] ' + marker + '"'})},
        "escaped_outer_credential": {"log": json.dumps(json.dumps({"entry": "passwd='" + json.dumps({"value": marker}) + "'"}))[1:-1]},
    }
    if case.startswith("environment_json_"):
        secret = json.dumps([{"name": "sid", "value": marker}])
        monkeypatch.setenv("FRESH_REVIEW_COOKIE", secret)
        payload = {
            "environment_json_dynamic_key": {secret: "ordinary"},
            "environment_json_value": {"log": secret},
            "environment_json_nested_value": {"log": json.dumps({"entry": secret})},
            "environment_json_nested_key": {"log": json.dumps({secret: "ordinary"})},
            "environment_json_escaped_value": {"log": json.dumps(json.dumps({"entry": secret}))},
            "environment_json_escaped_fragment": {"log": json.dumps(json.dumps({secret: secret}))[1:-1]},
        }[case]
    else:
        payload = payloads[case]

    async def handler(args, ctx):
        effects.append(1)
        return payload

    tools[0]["handler"] = tools[1]["handler"] = handler
    read = rpc(client, "review_read")
    arguments = {"request_id": str(uuid4())}
    write = rpc(client, "review_write", arguments, token=WRITE)
    replay = rpc(client, "review_write", arguments, token=WRITE)
    receipt = rpc(client, "get_invocation", arguments, token=WRITE)
    with sqlite3.connect(tmp_path / "state" / "invocations.sqlite3") as db:
        persisted = db.execute("SELECT response_json FROM invocations WHERE request_id=?", (arguments["request_id"],)).fetchone()[0]
    outputs = {"read": read.text, "write": write.text, "replay": replay.text, "receipt_api": receipt.text, "sqlite": persisted}
    assert all(response.status_code == 200 and response.json()["result"]["isError"] is False for response in (read, write, replay, receipt))
    assert effects == [1, 1]
    assert json.loads(persisted) == write.json()["result"]
    assert replay.json()["result"] == write.json()["result"]
    assert receipt.json()["result"]["structuredContent"]["result"] == write.json()["result"]
    leaks = [surface for surface, raw in outputs.items() if marker in raw]
    assert not leaks, {"case": case, "exposed_surfaces": leaks}
    if case.startswith("multiline_cookie_"):
        assert read.json()["result"]["structuredContent"]["log"] == "Cookie: [redacted]\nSafe event"


@pytest.mark.parametrize("text", [
    'token=[1, 2] usage={"prompt": 3} max_tokens=512 tokenizer=ordinary',
    'token={"prompt": 3, "completion": 14} token=17',
    'prompt_config={"max_tokens": 512, "token": 17} token_limit=2048',
])
def test_noncredential_log_configuration_keeps_its_complete_value(text):
    from src.mcp.redaction import redact

    assert redact(text) == text


def test_outer_credentials_keep_following_safe_fields_and_redaction_is_stable():
    from src.mcp.redaction import redact

    raw = 'passwd=[{"value": "private-array-fixture"}, "suffix"] safe=ordinary'
    cleaned = redact(raw)
    assert cleaned == 'passwd=[redacted] safe=ordinary'
    assert redact(cleaned) == cleaned
    raw = 'response: {"Cookie": "sid=private-cookie-fixture", "usage": {"token": 17}}'
    cleaned = redact(raw)
    assert json.loads(cleaned.removeprefix("response: ")) == {"Cookie": "[redacted]", "usage": {"token": 17}}
    assert redact(cleaned) == cleaned
