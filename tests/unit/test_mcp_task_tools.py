"""MCP business-tool tests: isolated SQLite, no crawler/LLM/network."""
import importlib
import importlib.util
import sqlite3
from pathlib import Path

import pytest
from fastapi import HTTPException
from pydantic import ValidationError


@pytest.fixture()
def mcp_context(api_context, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    prompts = tmp_path / "prompts"
    prompts.mkdir(exist_ok=True)
    # This fixture owns its isolated prompt catalog, independent of REST examples.
    for filename in ("sony_a7m4_criteria.txt", "sony_a7cr_criteria.txt"):
        (prompts / filename).unlink(missing_ok=True)
    (prompts / "base_prompt.txt").write_text("Base {{CRITERIA_SECTION}}", encoding="utf-8")
    (prompts / "criteria.txt").write_text("Only normal functioning devices", encoding="utf-8")
    api_context["app"].state.mcp_prompts_dir = prompts
    return {"app": api_context["app"], "principal": {"subject": "test", "scopes": {"tasks:read", "tasks:write", "tasks:run", "tasks:delete", "prompts:read", "prompts:write", "prompts:delete"}}}


async def invoke(name, payload, ctx):
    module = importlib.import_module("src.mcp.task_tools")
    tools = {tool["name"]: tool for tool in module.build_tools()}
    assert name in tools, f"missing business tool: {name}"
    tool = tools[name]
    return await tool["handler"](tool["input_model"].model_validate(payload), ctx)


def task_payload(**changes):
    return {"task_name": "camera", "keyword": "sony a7m4", "description": "Only normal functioning devices", "ai_prompt_criteria_file": "prompts/criteria.txt", **changes}


@pytest.mark.asyncio
async def test_create_read_sqlite_defaults_disabled(mcp_context, api_context):
    assert importlib.util.find_spec("src.mcp.task_tools") is not None, "MCP task tools not implemented"
    created = await invoke("create_task", task_payload(), mcp_context)
    task = created["task"]
    assert task["enabled"] is False and task["is_running"] is False
    assert task["revision"]
    actual = await invoke("get_task", {"task_id": task["id"]}, mcp_context)
    assert actual == created
    with sqlite3.connect(api_context["db_path"]) as conn:
        assert conn.execute("SELECT enabled,keyword FROM tasks WHERE id=?", (task["id"],)).fetchone() == (0, "sony a7m4")
    assert api_context["process_service"].started == []
    assert api_context["scheduler_service"].reload_calls == 1


@pytest.mark.asyncio
async def test_flat_keyword_crud_contract_without_prompt_assets(mcp_context):
    for path in Path("prompts").glob("*.txt"):
        path.unlink()
    created = await invoke("create_task", {"task_name": "keyword monitor", "keyword": "camera", "decision_mode": "keyword", "keyword_rules": ["working"], "enabled": False}, mcp_context)
    assert created["task"]["id"] == 0 and created["revision"] == created["task"]["revision"]
    readback = await invoke("get_task", {"task_id": 0}, mcp_context)
    assert readback == created
    changed = await invoke("update_task", {"task_id": 0, "expected_revision": created["revision"], "changes": {"task_name": "renamed"}}, mcp_context)
    assert changed["task"]["task_name"] == "renamed" and changed["revision"] != created["revision"]
    preview = await invoke("preview_task_deletion", {"task_id": 0}, mcp_context)
    assert preview["revision"] == changed["revision"]
    assert (await invoke("delete_task", {"task_id": 0, "expected_revision": preview["revision"]}, mcp_context))["deleted"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"min_price": "200", "max_price": "100"}, {"min_price": "NaN"},
    {"max_price": "-1"}, {"max_price": "infinity"}, {"max_pages": 0},
    {"task_name": " "}, {"keyword": " "}, {"cron": "bad"},
    {"ai_prompt_criteria_file": ""}, {"ai_prompt_criteria_file": "prompts/missing.txt"},
    {"ai_prompt_base_file": "../outside.txt"}, {"ai_prompt_criteria_file": "prompts/empty.txt"},
    {"account_strategy": "fixed", "account_state_file": None}, {"unexpected": True},
])
async def test_create_rejects_invalid_without_persistence(mcp_context, changes):
    (Path("prompts") / "empty.txt").write_text(" \n", encoding="utf-8")
    with pytest.raises((ValidationError, HTTPException)):
        await invoke("create_task", task_payload(**changes), mcp_context)
    from src.api import dependencies as deps
    service = mcp_context["app"].dependency_overrides[deps.get_task_service]()
    assert await service.get_all_tasks() == []


@pytest.mark.asyncio
async def test_update_preview_revision_and_criteria_sync(mcp_context, monkeypatch):
    from src.api.routes import tasks
    async def forbidden_ai(**kwargs):
        pytest.fail("ordinary MCP update invoked AI")
    monkeypatch.setattr(tasks, "generate_criteria", forbidden_ai)
    original = (await invoke("create_task", task_payload(), mcp_context))["task"]
    arguments = {"task_id": original["id"], "expected_revision": original["revision"], "changes": {"min_price": "10"}}
    preview = await invoke("preview_task_change", arguments, mcp_context)
    assert preview["changes"]["min_price"] == {"before": None, "after": "10"}
    assert (await invoke("get_task", {"task_id": original["id"]}, mcp_context))["task"] == original
    updated = (await invoke("update_task", arguments, mcp_context))["task"]
    assert updated["min_price"] == "10" and updated["revision"] != original["revision"]
    with pytest.raises(HTTPException) as stale:
        await invoke("update_task", arguments, mcp_context)
    assert stale.value.detail == "revision_conflict"
    args = {"task_id": original["id"], "expected_revision": updated["revision"], "changes": {"description": "No repairs"}}
    with pytest.raises(HTTPException) as unsynced:
        await invoke("update_task", args, mcp_context)
    assert unsynced.value.detail == "criteria_sync_required"
    args["criteria_content"] = "No repairs"
    synchronized = (await invoke("update_task", args, mcp_context))["task"]
    assert synchronized["description"] == "No repairs"
    assert Path(synchronized["ai_prompt_criteria_file"]).read_text() == "No repairs"
    assert Path("prompts/criteria.txt").read_text() == "Only normal functioning devices"
    assert synchronized["ai_prompt_criteria_file"] != "prompts/criteria.txt"


@pytest.mark.asyncio
async def test_list_clone_and_validate(mcp_context):
    original = (await invoke("create_task", task_payload(enabled=True), mcp_context))["task"]
    cloned = (await invoke("clone_task", {"task_id": original["id"], "expected_revision": original["revision"], "task_name": "camera copy"}, mcp_context))["task"]
    assert cloned["enabled"] is False and cloned["id"] != original["id"]
    assert cloned["ai_prompt_criteria_file"] != original["ai_prompt_criteria_file"]
    listing = await invoke("list_tasks", {"keyword": "sony", "enabled": False, "is_running": False, "limit": 1, "offset": 0}, mcp_context)
    assert listing["total"] == 1 and listing["has_more"] is False
    assert [item["id"] for item in listing["items"]] == [cloned["id"]]
    valid = await invoke("validate_task_config", {"config": task_payload()}, mcp_context)
    assert valid["valid"] is True
    from src.api import dependencies as deps
    service = mcp_context["app"].dependency_overrides[deps.get_task_service]()
    assert len(await service.get_all_tasks()) == 2


@pytest.mark.asyncio
async def test_runtime_schedule_and_safe_delete(mcp_context, api_context):
    task = (await invoke("create_task", task_payload(), mcp_context))["task"]
    args = {"task_id": task["id"], "expected_revision": task["revision"]}
    with pytest.raises(HTTPException) as disabled:
        await invoke("start_task", args, mcp_context)
    assert disabled.value.detail == "task_disabled"
    task = (await invoke("set_task_enabled", args | {"enabled": True}, mcp_context))["task"]
    args["expected_revision"] = task["revision"]
    task = (await invoke("set_task_schedule", args | {"cron": "0 */6 * * *"}, mcp_context))["task"]
    assert task["next_run_at"]
    args["expected_revision"] = task["revision"]
    started = (await invoke("start_task", args, mcp_context))["task"]
    assert started["is_running"] is True and started["revision"] == task["revision"]
    runtime = await invoke("get_task_runtime_status", {"task_id": task["id"]}, mcp_context)
    assert runtime["is_running"] is True
    stopped = (await invoke("stop_task", args, mcp_context))["task"]
    assert stopped["is_running"] is False
    # Import history through the real SQLite bootstrap, then preserve it on delete.
    with sqlite3.connect(api_context["db_path"]) as conn:
        from src.infrastructure.persistence.sqlite_bootstrap import _insert_result_record, _insert_price_snapshot
        _insert_result_record(conn, {"任务名称": "camera", "商品信息": {"商品ID": "1"}}, keyword="sony a7m4", filename="sony_a7m4_full_data.jsonl")
        _insert_price_snapshot(conn, {"keyword": "sony a7m4", "price": 100, "item_id": "1", "run_id": "r"})
    Path("logs").mkdir()
    Path("logs/task.log").write_text("preserve log")
    preview = await invoke("preview_task_deletion", {"task_id": task["id"]}, mcp_context)
    assert preview["preserves"] == ["results", "price_history", "logs", "prompts"]
    deleted = await invoke("delete_task", args, mcp_context)
    assert deleted["deleted"] is True
    with sqlite3.connect(api_context["db_path"]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM result_items").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM price_snapshots").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    assert Path("logs/task.log").read_text() == "preserve log"
    assert Path("prompts/criteria.txt").exists()


@pytest.mark.asyncio
async def test_purge_rejected_without_any_mutation(mcp_context):
    task = (await invoke("create_task", task_payload(), mcp_context))["task"]
    with pytest.raises(HTTPException) as exc:
        await invoke("delete_task", {"task_id": task["id"], "expected_revision": task["revision"], "purge": True}, mcp_context)
    assert exc.value.detail == "purge_not_supported"
    assert (await invoke("get_task", {"task_id": task["id"]}, mcp_context))["task"] == task


@pytest.mark.asyncio
async def test_generation_is_202_job_with_isolated_criteria(mcp_context, api_context, monkeypatch):
    from src.services import mcp_task_operations as ops
    assert hasattr(ops, "generate_task"), "safe task generation not implemented"
    queue = []
    generation = api_context["task_generation_service"]
    monkeypatch.setattr(generation, "track", lambda coroutine, *, job_id: queue.append(coroutine))
    async def fake_criteria(**kwargs):
        return "Generated only for this task"
    monkeypatch.setattr(ops, "generate_criteria", fake_criteria)
    Path("prompts/macbook_criteria.txt").write_text("reference")
    legacy = Path("prompts/sony_a7m4_criteria.txt")
    legacy.write_text("Must never be overwritten")
    result = await invoke("generate_task", {"task_name": "generated", "keyword": "sony a7m4", "description": "Only working cameras"}, mcp_context)
    assert result["status_code"] == 202 and result["job"]["status"] == "queued"
    assert len(queue) == 1
    await queue.pop()
    job = (await invoke("get_generation_job", {"job_id": result["job"]["job_id"]}, mcp_context))["job"]
    assert job["status"] == "completed"
    assert job["task"]["enabled"] is False
    assert Path(job["task"]["ai_prompt_criteria_file"]).read_text() == "Generated only for this task"
    assert job["task"]["ai_prompt_criteria_file"] != str(legacy)
    assert legacy.read_text() == "Must never be overwritten"
    assert api_context["process_service"].started == []


@pytest.mark.asyncio
async def test_generation_failure_preserves_sibling_and_redacts_error(mcp_context, api_context, monkeypatch):
    from src.services import mcp_task_operations as ops
    assert hasattr(ops, "generate_task"), "safe task generation not implemented"
    queue = []
    monkeypatch.setattr(api_context["task_generation_service"], "track", lambda coroutine, *, job_id: queue.append(coroutine))
    async def failure(**kwargs):
        raise RuntimeError("secret-provider-key")
    monkeypatch.setattr(ops, "generate_criteria", failure)
    Path("prompts/macbook_criteria.txt").write_text("reference")
    legacy = Path("prompts/sony_a7m4_criteria.txt")
    legacy.write_text("existing criteria")
    result = await invoke("generate_task", {"task_name": "failed", "keyword": "sony a7m4", "description": "Only working cameras"}, mcp_context)
    await queue.pop()
    job = (await invoke("get_generation_job", {"job_id": result["job"]["job_id"]}, mcp_context))["job"]
    assert job["status"] == "failed" and "secret-provider-key" not in str(job)
    assert legacy.read_text() == "existing criteria"


@pytest.mark.asyncio
async def test_concurrent_revision_writers_only_one_succeeds(mcp_context):
    import asyncio
    original = (await invoke("create_task", task_payload(), mcp_context))["task"]
    args = {"task_id": original["id"], "expected_revision": original["revision"]}
    results = await asyncio.gather(*[invoke("update_task", args | {"changes": {"max_price": price}}, mcp_context) for price in ("10", "20")], return_exceptions=True)
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, HTTPException) and result.detail == "revision_conflict" for result in results) == 1


@pytest.mark.asyncio
async def test_duplicate_task_names_cannot_select_wrong_crawler_task(mcp_context):
    await invoke("create_task", task_payload(), mcp_context)
    with pytest.raises(HTTPException) as exc:
        await invoke("create_task", task_payload(keyword="different keyword"), mcp_context)
    assert exc.value.detail == "task_name_in_use"


@pytest.mark.asyncio
async def test_update_preserves_account_policy_and_rejects_runtime_field(mcp_context):
    task = (await invoke("create_task", task_payload(), mcp_context))["task"]
    args = {"task_id": task["id"], "expected_revision": task["revision"]}
    with pytest.raises(HTTPException) as runtime:
        await invoke("update_task", args | {"changes": {"is_running": True}}, mcp_context)
    assert runtime.value.detail == "runtime_field_not_writable"
    task = (await invoke("update_task", args | {"changes": {"account_state_file": "state/account.json"}}, mcp_context))["task"]
    assert task["account_strategy"] == "fixed"
    args["expected_revision"] = task["revision"]
    task = (await invoke("update_task", args | {"changes": {"account_strategy": "ass"}}, mcp_context))["task"]
    assert task["account_strategy"] == "ass" and task["account_state_file"] is None


@pytest.mark.asyncio
async def test_prompt_content_changes_invalidate_task_revision(mcp_context):
    task = (await invoke("create_task", task_payload(), mcp_context))["task"]
    Path(task["ai_prompt_criteria_file"]).write_text("changed outside MCP")
    with pytest.raises(HTTPException) as exc:
        await invoke("set_task_enabled", {"task_id": task["id"], "expected_revision": task["revision"], "enabled": True}, mcp_context)
    assert exc.value.detail == "revision_conflict"


@pytest.mark.asyncio
async def test_failed_criteria_save_removes_only_new_asset(mcp_context, monkeypatch):
    from src.api import dependencies as deps
    service = mcp_context["app"].dependency_overrides[deps.get_task_service]()
    task = (await invoke("create_task", task_payload(), mcp_context))["task"]
    before = set(Path("prompts").iterdir())
    async def fail_save(*args, **kwargs):
        raise RuntimeError("simulated sqlite failure")
    monkeypatch.setattr(service, "update_task", fail_save)
    with pytest.raises(RuntimeError):
        await invoke("update_task", {"task_id": task["id"], "expected_revision": task["revision"], "changes": {"description": "new criteria"}, "criteria_content": "new criteria"}, mcp_context)
    assert set(Path("prompts").iterdir()) == before
    assert (await service.get_task(task["id"])).description == task["description"]


@pytest.mark.asyncio
async def test_duplicate_clone_and_generation_do_not_write_assets_or_enqueue(mcp_context, api_context, monkeypatch):
    task = (await invoke("create_task", task_payload(), mcp_context))["task"]
    before = set(Path("prompts").iterdir())
    with pytest.raises(HTTPException) as exc:
        await invoke("clone_task", {"task_id": task["id"], "expected_revision": task["revision"], "task_name": task["task_name"]}, mcp_context)
    assert exc.value.detail == "task_name_in_use"
    assert set(Path("prompts").iterdir()) == before
    Path("prompts/macbook_criteria.txt").write_text("reference")
    queue = []
    monkeypatch.setattr(api_context["task_generation_service"], "track", lambda coroutine, *, job_id: queue.append(coroutine))
    try:
        with pytest.raises(HTTPException) as exc:
            await invoke("generate_task", {"task_name": task["task_name"], "keyword": "sony", "description": "requirements"}, mcp_context)
        assert exc.value.detail == "task_name_in_use" and not queue
    finally:
        for coroutine in queue:
            coroutine.close()


def test_http_keyword_crud_receipts_against_real_sqlite(mcp_context, api_context, monkeypatch, tmp_path):
    from uuid import uuid4
    from fastapi.testclient import TestClient
    from src.mcp.remote import router
    token = "local-task-http-fixture-" + "x" * 40
    for key, value in {"GOOFISH_MCP_ENABLED": "true", "GOOFISH_MCP_WRITE_TOKEN": token, "GOOFISH_MCP_READ_TOKEN": "local-read-fixture-" + "r" * 40, "GOOFISH_MCP_WRITE_SCOPES": "tasks:read,tasks:write,tasks:delete", "GOOFISH_MCP_ALLOWED_HOSTS": "testserver", "GOOFISH_MCP_STATE_DIR": str(tmp_path / "receipts")}.items():
        monkeypatch.setenv(key, value)
    mcp_context["app"].include_router(router)
    with TestClient(mcp_context["app"]) as client:
        def call(name, arguments, write=False):
            if write:
                arguments = {**arguments, "request_id": str(uuid4())}
            body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
            headers = {"Authorization": "Bearer " + token, "Accept": "application/json, text/event-stream"}
            response = client.post("/api/mcp", json=body, headers=headers)
            assert response.status_code == 200
            result = response.json()["result"]
            assert result["isError"] is False, result
            if write:
                assert result["structuredContent"]["_receipt"]["status"] == "succeeded"
                assert client.post("/api/mcp", json=body, headers=headers).json()["result"] == result
            return result["structuredContent"]
        created = call("create_task", {"task_name": "http keyword", "keyword": "camera", "enabled": False, "decision_mode": "keyword", "keyword_rules": ["working"]}, write=True)
        assert created["task"]["id"] == 0
        assert call("get_task", {"task_id": 0})["revision"] == created["revision"]
        changed = call("update_task", {"task_id": 0, "expected_revision": created["revision"], "changes": {"task_name": "renamed over HTTP"}}, write=True)
        preview = call("preview_task_deletion", {"task_id": 0})
        assert preview["revision"] == changed["revision"]
        assert call("delete_task", {"task_id": 0, "expected_revision": preview["revision"]}, write=True)["deleted"] is True
    with sqlite3.connect(api_context["db_path"]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    assert api_context["process_service"].started == []
