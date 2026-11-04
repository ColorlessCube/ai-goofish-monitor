"""Read-only-review regressions. Only fixture SQLite/files and ASGI HTTP.
Portable deterministic interleavings, migrated from the independent review.
Run: pytest tests/unit/test_task_prompt_regressions.py (no external services).
"""
import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException

from test_mcp_task_tools import mcp_context, invoke, task_payload
from test_mcp_prompt_tools import prompt
from src.api import dependencies as deps
from src.services import mcp_task_operations as ops
from src.mcp import prompt_tools


def service(ctx):
    return ctx["app"].dependency_overrides[deps.get_task_service]()


async def rest(ctx, method, path, payload):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ctx["app"]), base_url="http://review.local") as client:
        result = await client.request(method, path, json=payload)
    assert result.status_code == 200, result.text
    return result.json()


async def settle(coro):
    try:
        return await coro
    except HTTPException as exc:
        return {"error": exc.detail, "status": exc.status_code}


@pytest.mark.asyncio
async def test_rest_update_between_revision_check_and_save_must_not_be_overwritten(mcp_context, monkeypatch):
    original = await invoke("create_task", task_payload(), mcp_context)
    checked, release = asyncio.Event(), asyncio.Event()
    prepare = ops.prepare_update

    async def pause_after_check(*args):
        result = await prepare(*args)
        checked.set()
        await release.wait()
        return result

    monkeypatch.setattr(ops, "prepare_update", pause_after_check)
    write = asyncio.create_task(settle(invoke("update_task", {"task_id": 0, "expected_revision": original["revision"], "changes": {"max_price": "500"}}, mcp_context)))
    await asyncio.wait_for(checked.wait(), 2)
    rest_result = await rest(mcp_context, "PATCH", "/api/tasks/0", {"min_price": "200"})
    release.set()
    mcp_result = await asyncio.wait_for(write, 2)
    actual = await service(mcp_context).get_task(0)
    print(json.dumps({"rest_min_price": rest_result["task"]["min_price"], "mcp_error": mcp_result.get("error"), "final_min_price": actual.min_price, "final_max_price": actual.max_price}))
    assert actual.min_price == "200", "MCP accepted stale revision and replaced REST's committed min_price with None"
    assert mcp_result.get("error") == "revision_conflict"


@pytest.mark.asyncio
async def test_rest_update_during_stop_must_invalidate_task_delete(mcp_context, api_context, monkeypatch):
    original = await invoke("create_task", task_payload(), mcp_context)
    checked, release = asyncio.Event(), asyncio.Event()
    stop = api_context["process_service"].stop_task

    async def pause_after_stop(task_id):
        await stop(task_id)
        checked.set()
        await release.wait()

    monkeypatch.setattr(api_context["process_service"], "stop_task", pause_after_stop)
    deletion = asyncio.create_task(settle(invoke("delete_task", {"task_id": 0, "expected_revision": original["revision"]}, mcp_context)))
    await asyncio.wait_for(checked.wait(), 2)
    await rest(mcp_context, "PATCH", "/api/tasks/0", {"task_name": "changed after deletion preview"})
    release.set()
    result = await asyncio.wait_for(deletion, 2)
    actual = await service(mcp_context).get_task(0)
    print(json.dumps({"delete_result": result, "task_still_exists": actual is not None}))
    assert result.get("error") == "revision_conflict", "MCP deleted a REST-modified task using stale deletion revision"
    assert actual is not None


@pytest.mark.asyncio
async def test_rest_reference_created_after_prompt_scan_must_block_delete(mcp_context, monkeypatch):
    created = await prompt("create_prompt", {"filename": "orphan.txt", "content": "criteria"}, mcp_context)
    checked, release = asyncio.Event(), asyncio.Event()
    references = prompt_tools.references

    async def pause_after_scan(*args):
        result = await references(*args)
        checked.set()
        await release.wait()
        return result

    monkeypatch.setattr(prompt_tools, "references", pause_after_scan)
    deletion = asyncio.create_task(settle(prompt("delete_prompt", {"filename": "orphan.txt", "expected_revision": created["revision"]}, mcp_context)))
    await asyncio.wait_for(checked.wait(), 2)
    await rest(mcp_context, "POST", "/api/tasks/", task_payload(ai_prompt_criteria_file="prompts/orphan.txt", enabled=False))
    release.set()
    result = await asyncio.wait_for(deletion, 2)
    task = await service(mcp_context).get_task(0)
    print(json.dumps({"delete_result": result, "persisted_reference": task.ai_prompt_criteria_file, "asset_exists": Path(task.ai_prompt_criteria_file).exists()}))
    assert result.get("error") in {"revision_conflict", "prompt_in_use"}, "MCP deleted newly referenced criteria and left persisted task dangling"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["update_prompt", "delete_prompt"])
async def test_rest_prompt_write_between_read_and_mutation_must_conflict(mcp_context, monkeypatch, action):
    from src.api.routes import prompts as rest_prompts
    monkeypatch.setattr(rest_prompts, "_PROMPTS_DIR", Path("prompts").absolute())
    mcp_context["app"].include_router(rest_prompts.router)
    created = await prompt("create_prompt", {"filename": "orphan.txt", "content": "original"}, mcp_context)
    checked, release = asyncio.Event(), asyncio.Event()
    references = prompt_tools.references
    calls = 0

    async def pause_first_scan(*args):
        nonlocal calls
        result = await references(*args)
        calls += 1
        if calls == 1:
            checked.set()
            await release.wait()
        return result

    monkeypatch.setattr(prompt_tools, "references", pause_first_scan)
    args = {"filename": "orphan.txt", "expected_revision": created["revision"]}
    if action == "update_prompt":
        args["content"] = "MCP replacement"
    mutation = asyncio.create_task(settle(prompt(action, args, mcp_context)))
    await asyncio.wait_for(checked.wait(), 2)
    await rest(mcp_context, "PUT", "/api/prompts/orphan.txt", {"content": "REST committed replacement"})
    release.set()
    result = await asyncio.wait_for(mutation, 2)
    path = Path("prompts/orphan.txt")
    print(json.dumps({"action": action, "mcp_result": result, "file": path.read_text() if path.exists() else None}))
    assert result.get("error") == "revision_conflict", "MCP accepted old content revision after REST changed the file"


@pytest.mark.asyncio
async def test_stale_task_preview_must_not_delete_recreated_task(mcp_context):
    original = await invoke("create_task", task_payload(), mcp_context)
    preview = await invoke("preview_task_deletion", {"task_id": 0}, mcp_context)
    await invoke("delete_task", {"task_id": 0, "expected_revision": original["revision"]}, mcp_context)
    replacement = await invoke("create_task", task_payload(), mcp_context)
    result = await settle(invoke("delete_task", {"task_id": preview["task_id"], "expected_revision": preview["revision"]}, mcp_context))
    print(json.dumps({"old_id": original["task"]["id"], "new_id": replacement["task"]["id"], "same_revision": original["revision"] == replacement["revision"], "old_preview_delete": result}))
    assert result.get("error") in {"revision_conflict", "task_not_found"}, "A deleted task's preview/revision authorizes deleting a distinct recreated task"


@pytest.mark.asyncio
async def test_generation_job_must_not_resolve_to_reused_task_id(mcp_context, api_context, monkeypatch):
    queue = []
    monkeypatch.setattr(api_context["task_generation_service"], "track", lambda coroutine, *, job_id: queue.append(coroutine))
    result = await invoke("generate_task", {"task_name": "original generated", "keyword": "original", "decision_mode": "keyword", "keyword_rules": ["working"]}, mcp_context)
    await queue.pop()
    job_id = result["job"]["job_id"]
    completed = await invoke("get_generation_job", {"job_id": job_id}, mcp_context)
    task = completed["job"]["task"]
    await invoke("delete_task", {"task_id": task["id"], "expected_revision": task["revision"]}, mcp_context)
    missing = await settle(invoke("get_generation_job", {"job_id": job_id}, mcp_context))
    await invoke("create_task", task_payload(task_name="unrelated later task"), mcp_context)
    rebound = await invoke("get_generation_job", {"job_id": job_id}, mcp_context)
    print(json.dumps({"after_original_deleted": missing, "job_name": rebound["job"]["task_name"], "returned_task_name": rebound["job"]["task"]["task_name"]}))
    assert rebound["job"]["task"]["task_name"] == "original generated", "Old completed job returns unrelated reused-ID task"


@pytest.mark.asyncio
async def test_clone_keyword_task_with_stale_unused_criteria(mcp_context):
    original = await invoke("create_task", {"task_name": "keyword only", "keyword": "camera", "decision_mode": "keyword", "keyword_rules": ["working"], "ai_prompt_criteria_file": "prompts/missing.txt"}, mcp_context)
    result = await settle(invoke("clone_task", {"task_id": original["task"]["id"], "expected_revision": original["revision"], "task_name": "keyword clone"}, mcp_context))
    print(json.dumps(result))
    assert "task" in result, "Valid keyword task cannot be cloned because unused missing AI asset is read"


@pytest.mark.asyncio
@pytest.mark.parametrize("action,extra", [("set_task_enabled", {"enabled": False}), ("set_task_schedule", {"cron": None})])
async def test_disable_or_unschedule_must_work_when_ai_asset_is_missing(mcp_context, action, extra):
    await invoke("create_task", task_payload(enabled=True, cron="0 * * * *"), mcp_context)
    Path("prompts/criteria.txt").unlink()
    current = await invoke("get_task", {"task_id": 0}, mcp_context)
    result = await settle(invoke(action, {"task_id": 0, "expected_revision": current["revision"], **extra}, mcp_context))
    task = await service(mcp_context).get_task(0)
    print(json.dumps({"action": action, "result": result, "enabled": task.enabled, "cron": task.cron}))
    assert "task" in result, "Safety action is blocked by unrelated missing AI criteria; enabled cron remains active"


@pytest.mark.asyncio
async def test_validated_prompt_file_must_be_the_file_the_crawler_reads(mcp_context):
    Path("criteria.txt").write_text("different CWD criteria, not the reviewed prompt")
    created = await invoke("create_task", task_payload(ai_prompt_criteria_file="criteria.txt"), mcp_context)
    saved = created["task"]["ai_prompt_criteria_file"]
    reviewed = ops.read_prompt(mcp_context, saved)
    # Same file-read semantics as spider_v2.py lines 119-122; never run a crawler.
    consumed = Path(saved).read_text()
    print(json.dumps({"persisted_path": saved, "validated_and_hashed_content": reviewed, "runtime_file_content": consumed}))
    assert consumed == reviewed, "MCP hashes prompts/criteria.txt but persists criteria.txt; crawler reads another file"


@pytest.mark.asyncio
async def test_start_task_id_must_not_resolve_to_legacy_duplicate_name(mcp_context, api_context, monkeypatch):
    from src.infrastructure.persistence.sqlite_task_repository import find_task_by_name_sync
    monkeypatch.setenv("APP_DATABASE_FILE", str(api_context["db_path"]))
    first = await rest(mcp_context, "POST", "/api/tasks/", task_payload(task_name="duplicate", enabled=True, keyword="first keyword"))
    second = await rest(mcp_context, "POST", "/api/tasks/", task_payload(task_name="unique before legacy import", enabled=True, keyword="second keyword"))
    # Simulate a pre-upgrade database with duplicate names; new writes reject them.
    import sqlite3
    with sqlite3.connect(api_context["db_path"]) as conn:
        conn.execute("UPDATE tasks SET task_name='duplicate' WHERE id=?", (second["task"]["id"],))
    selected = await invoke("get_task", {"task_id": second["task"]["id"]}, mcp_context)
    result = await settle(invoke("start_task", {"task_id": selected["task"]["id"], "expected_revision": selected["revision"]}, mcp_context))
    started = api_context["process_service"].started
    actual = find_task_by_name_sync(started[0][1]) if started else None
    print(json.dumps({"selected_id": second["task"]["id"], "mock_start_arguments": started, "name_lookup_id": actual.id if actual else None, "error": result.get("error")}))
    assert not started or actual.id == selected["task"]["id"], "Start accepted ID 1 but --task-name resolves ID 0"


@pytest.mark.asyncio
async def test_generation_failure_after_persistence_must_expose_created_task(mcp_context, api_context, monkeypatch):
    queue = []
    monkeypatch.setattr(api_context["task_generation_service"], "track", lambda coroutine, *, job_id: queue.append(coroutine))
    Path("prompts/macbook_criteria.txt").write_text("reference")
    async def fake_criteria(**kwargs):
        return "generated isolated criteria"
    async def failed_scheduler(*args):
        raise RuntimeError("scheduler unavailable after SQLite commit")
    monkeypatch.setattr(ops, "generate_criteria", fake_criteria)
    monkeypatch.setattr(api_context["scheduler_service"], "reload_jobs", failed_scheduler)
    accepted = await invoke("generate_task", {"task_name": "partial generation", "keyword": "camera", "description": "working only"}, mcp_context)
    await queue.pop()
    job = (await invoke("get_generation_job", {"job_id": accepted["job"]["job_id"]}, mcp_context))["job"]
    actual = await service(mcp_context).get_all_tasks()
    print(json.dumps({"reported_job": job, "persisted_tasks": [{"id": task.id, "enabled": task.enabled, "criteria_exists": Path(task.ai_prompt_criteria_file).exists()} for task in actual]}))
    assert not actual or "task" in job, "Failed generation hides its committed task and retained criteria asset"
