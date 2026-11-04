"""Payload budgeting must never erase durable mutation facts."""
import json
import sqlite3
from uuid import uuid4

import pytest

from src.mcp.outcomes import MutationOutcomeError
from src.mcp.redaction import MAX_OUTPUT_BYTES
from tests.unit.test_mcp_remote_review import WRITE, rpc, setup  # noqa: F401


@pytest.mark.parametrize("status", ["succeeded", "failed", "unknown"])
def test_receipt_survives_appending_metadata_at_output_limit(setup, status):
    client, tools, effects, tmp_path = setup
    payload = {"large": "x" * (MAX_OUTPUT_BYTES - 100)}
    assert len(json.dumps(payload).encode()) < MAX_OUTPUT_BYTES

    async def handler(args, ctx):
        effects.append(1)
        if status != "succeeded":
            raise MutationOutcomeError(status, payload)
        return payload

    tools[1]["handler"] = handler
    arguments = {"request_id": str(uuid4())}
    first = rpc(client, "review_write", arguments, token=WRITE)
    replay = rpc(client, "review_write", arguments, token=WRITE)
    queried = rpc(client, "get_invocation", arguments, token=WRITE)
    with sqlite3.connect(tmp_path / "state" / "invocations.sqlite3") as db:
        row = db.execute("SELECT status,response_json FROM invocations WHERE request_id=?", (arguments["request_id"],)).fetchone()
    result = first.json()["result"]
    assert first.status_code == replay.status_code == queried.status_code == 200
    assert replay.json()["result"] == result
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
    assert row[0] == status
    assert json.loads(row[1]) == result
    content = queried.json()["result"]["structuredContent"]
    assert content["status"] == status and content["retry_allowed"] is False
    assert effects == [1]
    receipt = result["structuredContent"].get("_receipt", {})
    assert receipt.get("status") == status, result
    assert receipt["retry_allowed"] is False
    assert receipt["request_id"] == arguments["request_id"]
    assert receipt["invocation_id"] == content["invocation_id"]
    assert content["result"] == result
    assert result["isError"] is (status != "succeeded")


@pytest.mark.parametrize("status", ["succeeded", "failed", "unknown"])
@pytest.mark.parametrize("delta", [-1, 0, 1])
@pytest.mark.parametrize("unit", ["x", "凭据🚀"])
def test_receipt_budget_counts_utf8_payload_once_including_metadata(setup, status, delta, unit):
    client, tools, effects, tmp_path = setup
    request_id = str(uuid4())
    template = {"large": "", "_receipt": {"invocation_id": str(uuid4()), "request_id": request_id, "status": status, "retry_allowed": False}}
    available = MAX_OUTPUT_BYTES + delta - len(json.dumps(template, ensure_ascii=False).encode("utf-8"))
    width = len(unit.encode("utf-8"))
    large = unit * (available // width) + "x" * (available % width)
    template["large"] = large
    assert len(json.dumps(template, ensure_ascii=False).encode("utf-8")) == MAX_OUTPUT_BYTES + delta

    async def handler(args, ctx):
        effects.append(1)
        if status != "succeeded":
            raise MutationOutcomeError(status, {"large": large})
        return {"large": large}

    tools[1]["handler"] = handler
    arguments = {"request_id": request_id}
    first = rpc(client, "review_write", arguments, token=WRITE).json()["result"]
    replay = rpc(client, "review_write", arguments, token=WRITE).json()["result"]
    lookup = rpc(client, "get_invocation", arguments, token=WRITE).json()["result"]
    content = first["structuredContent"]
    assert json.loads(first["content"][0]["text"]) == content
    assert first == replay
    assert first["isError"] is (status != "succeeded")
    assert len(json.dumps(content, ensure_ascii=False).encode("utf-8")) <= MAX_OUTPUT_BYTES
    assert content["_receipt"]["status"] == status
    assert content["_receipt"]["request_id"] == request_id
    assert content["_receipt"]["retry_allowed"] is False
    if delta <= 0:
        assert content["large"] == large
    else:
        assert "large" not in content and content["error"] == "output_limit_exceeded"
    queried = lookup["structuredContent"]
    assert json.loads(lookup["content"][0]["text"]) == queried
    assert lookup["isError"] is False
    assert queried["status"] == status and queried["retry_allowed"] is False
    assert queried["invocation_id"] == content["_receipt"]["invocation_id"]
    assert queried["result"] == first
    with sqlite3.connect(tmp_path / "state" / "invocations.sqlite3") as db:
        row = db.execute("SELECT status,response_json FROM invocations WHERE request_id=?", (request_id,)).fetchone()
    assert row[0] == status and json.loads(row[1]) == first
    assert effects == [1]


def test_truncation_uses_transport_receipt_not_handler_claims(setup):
    client, tools, effects, _ = setup

    async def handler(args, ctx):
        effects.append(1)
        return {"large": "x" * MAX_OUTPUT_BYTES, "status": "unknown", "retry_allowed": True,
                "_receipt": {"invocation_id": "forged", "request_id": "forged", "status": "failed", "retry_allowed": True}}

    tools[1]["handler"] = handler
    arguments = {"request_id": str(uuid4())}
    first = rpc(client, "review_write", arguments, token=WRITE).json()["result"]
    receipt = first["structuredContent"]["_receipt"]
    assert receipt["status"] == "succeeded" and receipt["retry_allowed"] is False
    assert receipt["request_id"] == arguments["request_id"]
    assert receipt["invocation_id"] != "forged"
    assert first["isError"] is False
    assert rpc(client, "review_write", arguments, token=WRITE).json()["result"] == first
    assert effects == [1]


@pytest.mark.parametrize("status", ["succeeded", "failed", "unknown"])
def test_historical_oversized_lookup_matches_replay_without_rewriting_row(setup, status):
    from src.mcp import journal

    client, _, effects, tmp_path = setup
    request_id = str(uuid4())
    _, row = journal.claim("writer:independent-review", request_id, "review_write", {})
    marker = "historical-budget-private-fixture"
    historical = {"structuredContent": {"large": "x" * MAX_OUTPUT_BYTES, "log": "passwd='" + marker + "'",
                                        "_receipt": {"status": "forged", "retry_allowed": True}},
                  "content": [{"type": "text", "text": marker}], "isError": status != "succeeded"}
    journal.finish(row["invocation_id"], status, historical)
    arguments = {"request_id": request_id}
    replay = rpc(client, "review_write", arguments, token=WRITE)
    lookup = rpc(client, "get_invocation", arguments, token=WRITE)
    queried = lookup.json()["result"]["structuredContent"]
    assert lookup.json()["result"]["isError"] is False
    assert queried["status"] == status and queried["retry_allowed"] is False
    assert queried["result"] == replay.json()["result"]
    receipt = queried["result"]["structuredContent"]["_receipt"]
    assert receipt == {"invocation_id": row["invocation_id"], "request_id": request_id, "status": status, "retry_allowed": False}
    assert marker not in replay.text and marker not in lookup.text
    with sqlite3.connect(tmp_path / "state" / "invocations.sqlite3") as db:
        persisted = db.execute("SELECT status,response_json FROM invocations WHERE request_id=?", (request_id,)).fetchone()
    assert persisted == (status, json.dumps(historical, ensure_ascii=False, allow_nan=False))
    assert effects == []
