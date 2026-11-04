"""Typed business outcomes cross the transport without redoing side effects."""
import importlib.util
import json
import sqlite3
from uuid import uuid4

import pytest

from tests.unit.test_mcp_remote_review import READ, WRITE, rpc, setup  # noqa: F401


def outcome_type():
    assert importlib.util.find_spec("src.mcp.outcomes") is not None, "typed mutation outcome interface is missing"
    from src.mcp.outcomes import MutationOutcomeError
    return MutationOutcomeError


@pytest.mark.parametrize("status", ["unknown", "failed"])
def test_typed_outcome_persists_safe_result_and_never_reexecutes(setup, status):
    client, tools, effects, tmp_path = setup
    outcome = outcome_type()
    marker = "outcome-fixture-private-903"
    payload = {"success": False, "reason": "delivery_not_confirmed", "api_key": marker,
               "log": "Authorization: Bearer " + marker,
               "_receipt": {"status": "succeeded", "retry_allowed": True}}
    exc = outcome(status, payload)
    assert exc.status == status and exc.payload is payload
    assert str(exc) == "mutation_outcome_error"
    assert marker not in repr(exc)

    async def mutate(args, ctx):
        effects.append(1)
        raise exc

    tools[1]["handler"] = mutate
    request_id = str(uuid4())
    arguments = {"request_id": request_id}
    first = rpc(client, "review_write", arguments, token=WRITE)
    replay = rpc(client, "review_write", arguments, token=WRITE)
    assert first.status_code == replay.status_code == 200
    result = first.json()["result"]
    assert replay.json()["result"] == result
    assert result["isError"] is True
    content = result["structuredContent"]
    assert content["reason"] == "delivery_not_confirmed"
    assert content["success"] is False
    assert content["_receipt"]["status"] == status
    assert content["_receipt"]["retry_allowed"] is False
    assert json.loads(result["content"][0]["text"]) == content
    receipt = rpc(client, "get_invocation", arguments, token=WRITE)
    queried = receipt.json()["result"]["structuredContent"]
    assert queried["status"] == status and queried["retry_allowed"] is False
    assert queried["result"] == result
    assert rpc(client, "get_invocation", arguments, token=READ).json()["result"]["isError"] is True
    with sqlite3.connect(tmp_path / "state" / "invocations.sqlite3") as db:
        rows = db.execute("SELECT status,response_json FROM invocations WHERE request_id=?", (request_id,)).fetchall()
    assert len(rows) == 1 and rows[0][0] == status
    assert json.loads(rows[0][1]) == result
    assert effects == [1]
    assert not any(marker in raw for raw in (first.text, replay.text, receipt.text, rows[0][1]))


@pytest.mark.parametrize("status", ["succeeded", "running", "FAILED", "", None, {}, 1])
def test_outcome_rejects_invalid_internal_status(status):
    with pytest.raises(ValueError, match="^invalid_mutation_outcome_status$"):
        outcome_type()(status, {})


def test_outcome_rejects_non_dict_payload():
    with pytest.raises(TypeError, match="^mutation_outcome_payload_must_be_dict$"):
        outcome_type()("unknown", "fixture-private-error")


@pytest.mark.parametrize("status", ["failed", "unknown"])
def test_plain_handler_dict_cannot_claim_a_typed_outcome(setup, status):
    client, tools, effects, _ = setup

    async def ordinary(args, ctx):
        effects.append(1)
        return {"status": status, "success": False, "payload": {"reason": "data_only"},
                "_receipt": {"status": status, "retry_allowed": True}}

    tools[1]["handler"] = ordinary
    arguments = {"request_id": str(uuid4())}
    first = rpc(client, "review_write", arguments, token=WRITE).json()["result"]
    assert first["isError"] is False
    assert first["structuredContent"]["status"] == status
    assert first["structuredContent"]["_receipt"]["status"] == "succeeded"
    assert first["structuredContent"]["_receipt"]["retry_allowed"] is False
    assert rpc(client, "review_write", arguments, token=WRITE).json()["result"] == first
    assert rpc(client, "get_invocation", arguments, token=WRITE).json()["result"]["structuredContent"]["status"] == "succeeded"
    assert effects == [1]


@pytest.mark.parametrize("status", ["failed", "unknown", "succeeded"])
@pytest.mark.parametrize("oversized", [False, True])
def test_historical_replay_rescrubs_and_bounds_without_rewriting_execution(setup, status, oversized):
    from src.mcp import journal
    client, _, effects, tmp_path = setup
    marker = "historical-fixture-private-903"
    request_id = str(uuid4())
    action, row = journal.claim("writer:independent-review", request_id, "review_write", {})
    assert action == "new"
    receipt = {"invocation_id": row["invocation_id"], "request_id": request_id, "status": status, "retry_allowed": False}
    structured = {"log": json.dumps({"Cookie": "sid=" + marker}), "_receipt": receipt}
    if oversized:
        structured["old_large_output"] = "x" * 300000
    # A prior version wrote raw data. Include an independent text-only leak so
    # regeneration must not pass old content through unchanged.
    historical = {"structuredContent": structured, "content": [{"type": "text", "text": "Cookie: sid=" + marker}], "isError": status != "succeeded"}
    journal.finish(row["invocation_id"], status, historical)
    before = json.dumps(historical, ensure_ascii=False, allow_nan=False)
    replay = rpc(client, "review_write", {"request_id": request_id}, token=WRITE)
    assert replay.status_code == 200
    result = replay.json()["result"]
    assert marker not in replay.text
    assert len(replay.content) < 120000
    assert result["isError"] == historical["isError"]
    assert result["structuredContent"]["_receipt"] == receipt
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
    if oversized:
        assert result["structuredContent"]["error"] == "output_limit_exceeded"
    else:
        queried = rpc(client, "get_invocation", {"request_id": request_id}, token=WRITE)
        assert marker not in queried.text
        assert queried.json()["result"]["structuredContent"]["status"] == status
        assert queried.json()["result"]["structuredContent"]["retry_allowed"] is False
    with sqlite3.connect(tmp_path / "state" / "invocations.sqlite3") as db:
        persisted = db.execute("SELECT status,response_json FROM invocations WHERE request_id=?", (request_id,)).fetchone()
    assert persisted == (status, before)
    assert effects == []


def test_replay_budgets_payload_not_the_two_serialized_copies(setup):
    from src.mcp.redaction import MAX_OUTPUT_BYTES
    client, tools, effects, _ = setup

    async def mutate(args, ctx):
        effects.append(1)
        return {"large_but_allowed": "x" * 70000}

    tools[1]["handler"] = mutate
    arguments = {"request_id": str(uuid4())}
    first = rpc(client, "review_write", arguments, token=WRITE)
    replay = rpc(client, "review_write", arguments, token=WRITE)
    result = first.json()["result"]
    assert len(first.content) > MAX_OUTPUT_BYTES
    assert len(json.dumps(result["structuredContent"]).encode()) < MAX_OUTPUT_BYTES
    assert result["isError"] is False
    assert replay.json()["result"] == result
    assert effects == [1]
