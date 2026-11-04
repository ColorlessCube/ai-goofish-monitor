"""Independent local-only probes; imports production code, never starts services."""
import asyncio
import fcntl
import os
from pathlib import Path
import threading

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from starlette.requests import Request

from test_mcp_task_tools import mcp_context, invoke, task_payload
from test_mcp_prompt_tools import prompt
from src.api import dependencies as deps
from src.api.routes import prompts as rest_prompts
from src.domain.models.task import TaskCreate, TaskUpdate
from src.infrastructure.persistence.sqlite_task_repository import SqliteTaskRepository
from src.services.task_service import TaskService
from src.services import mcp_task_operations as ops, prompt_assets as assets


@pytest.mark.asyncio
async def test_same_content_task_aba_rejects_old_revision(mcp_context):
    created = await invoke("create_task", task_payload(), mcp_context)
    service = mcp_context["app"].dependency_overrides[deps.get_task_service]()
    old = await service.get_task(0)
    await service.update_task(0, TaskUpdate(min_price="123"))
    current = await service.update_task(0, TaskUpdate(min_price=None))
    assert current.min_price == old.min_price
    assert current.config_version == old.config_version + 2
    with pytest.raises(HTTPException, match="revision_conflict"):
        await invoke("delete_task", {"task_id": 0, "expected_revision": created["revision"]}, mcp_context)
    assert await service.get_task(0) is not None


@pytest.mark.asyncio
async def test_concurrent_same_thread_coroutines_do_not_bypass_flock(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "prompts"
    root.mkdir()
    repo = SqliteTaskRepository(str(tmp_path / "db.sqlite"), None, root)
    service = TaskService(repo)
    await service.get_all_tasks()
    entered = threading.Event()
    release = threading.Event()
    def hold_lock():
        with assets.asset_lock(root):
            entered.set()
            assert release.wait(5)
    holder = asyncio.create_task(asyncio.to_thread(hold_lock))
    assert await asyncio.to_thread(entered.wait, 2)
    creation = asyncio.create_task(service.create_task(TaskCreate(task_name="blocked", keyword="x", decision_mode="keyword", keyword_rules=["x"])))
    ticks = 0
    try:
        for _ in range(10):
            await asyncio.sleep(.01)
            ticks += 1
        assert not creation.done(), "another coroutine bypassed flock"
        # Separate descriptor in this process must not acquire the locked inode.
        with assets.directory(root) as fd:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        release.set()
        await holder
    task = await asyncio.wait_for(creation, 3)
    assert task.id == 0 and ticks == 10


@pytest.mark.asyncio
async def test_clone_checks_revision_after_rest_interleaving(mcp_context, monkeypatch):
    created = await invoke("create_task", task_payload(), mcp_context)
    service = mcp_context["app"].dependency_overrides[deps.get_task_service]()
    checked, release = asyncio.Event(), asyncio.Event()
    original = ops.ensure_unique_name
    calls = 0
    async def pause(*args, **kwargs):
        nonlocal calls
        result = await original(*args, **kwargs)
        calls += 1
        if calls == 1:
            checked.set()
            await release.wait()
        return result
    monkeypatch.setattr(ops, "ensure_unique_name", pause)
    clone = asyncio.create_task(invoke("clone_task", {"task_id": 0, "expected_revision": created["revision"], "task_name": "clone"}, mcp_context))
    await asyncio.wait_for(checked.wait(), 2)
    # Cooperative REST asset write is valid while the MCP process-local lock is held.
    mcp_context["app"].include_router(rest_prompts.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=mcp_context["app"]), base_url="http://local") as client:
        result = await client.put("/api/prompts/criteria.txt", json={"content": "REST NEW CONTENT"})
    assert result.status_code == 200, result.text
    changed = await service.get_task(0)
    assert ops.task_revision(changed, mcp_context) != created["revision"]
    release.set()
    try:
        result = await asyncio.wait_for(clone, 3)
    except HTTPException as exc:
        assert exc.detail == "revision_conflict"
        return
    assert False, {"accepted_stale_clone": result["task"]["id"], "cloned_criteria": assets.read_prompt(result["task"]["ai_prompt_criteria_file"]), "reviewed_criteria": "Only normal functioning devices"}


@pytest.mark.asyncio
@pytest.mark.parametrize("use_app_override", [False, True])
async def test_rest_uses_configured_env_asset_root(tmp_path, monkeypatch, use_app_override):
    monkeypatch.chdir(tmp_path)
    default = tmp_path / "prompts"
    configured = tmp_path / "configured-prompts"
    default.mkdir()
    configured.mkdir()
    (default / "criteria.txt").write_text("DEFAULT")
    (configured / "criteria.txt").write_text("CONFIGURED")
    (default / "default-only.txt").write_text("default")
    (configured / "configured-only.txt").write_text("configured")
    monkeypatch.setenv("GOOFISH_PROMPTS_DIR", str(configured))
    monkeypatch.setattr(rest_prompts, "_PROMPTS_DIR", default)
    app = FastAPI()
    app.include_router(rest_prompts.router)
    if use_app_override:
        monkeypatch.setenv("GOOFISH_PROMPTS_DIR", str(default))
        app.state.mcp_prompts_dir = configured
    service = TaskService(SqliteTaskRepository(str(tmp_path / "db.sqlite"), None))
    app.dependency_overrides[deps.get_task_service] = lambda: service
    ctx = {"app": app}
    assert ops.prompt_root(ctx) == configured
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
        response = await client.put("/api/prompts/criteria.txt", json={"content": "REST UPDATE"})
        listing = await client.get("/api/prompts")
        readback = await client.get("/api/prompts/criteria.txt")
    assert response.status_code == 200, response.text
    observed = {"configured": (configured / "criteria.txt").read_text(), "default": (default / "criteria.txt").read_text(), "listed": listing.json()}
    assert observed["configured"] == "REST UPDATE" and observed["default"] == "DEFAULT", observed
    assert listing.json() == ["configured-only.txt", "criteria.txt"]
    assert readback.json()["content"] == "REST UPDATE"


@pytest.mark.asyncio
async def test_rest_rejects_clearing_ai_asset(mcp_context):
    await invoke("create_task", task_payload(enabled=True, cron="0 * * * *"), mcp_context)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=mcp_context["app"]), base_url="http://local") as client:
        response = await client.patch("/api/tasks/0", json={"ai_prompt_criteria_file": ""})
    saved = await mcp_context["app"].dependency_overrides[deps.get_task_service]().get_task(0)
    assert response.status_code in {400, 404, 409, 422}, {"status": response.status_code, "criteria": saved.ai_prompt_criteria_file, "enabled": saved.enabled, "cron": saved.cron}


def test_concurrent_old_schema_migration_and_highwater_across_processes(tmp_path, monkeypatch):
    import json
    import sqlite3
    import subprocess
    import sys
    from src.infrastructure.persistence.sqlite_connection import SCHEMA_STATEMENTS
    from src.domain.models.task import Task
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "old.sqlite"
    # Build the pre-CAS table from its still-legacy CREATE TABLE shape.
    with sqlite3.connect(db) as conn:
        conn.execute(SCHEMA_STATEMENTS[1])
        values = SqliteTaskRepository._task_values(Task(**TaskCreate(task_name="legacy-zero", keyword="x", decision_mode="keyword", keyword_rules=["x"]).model_dump(), id=0))
        values.pop("instance_id")
        values.pop("config_version")
        columns = ",".join(values)
        placeholders = ",".join(":" + key for key in values)
        conn.execute(f"INSERT INTO tasks ({columns}) VALUES ({placeholders})", values)
    child = '''import json,sys
from src.infrastructure.persistence.sqlite_task_repository import SqliteTaskRepository
r=SqliteTaskRepository(sys.argv[1], None)
t=r._find_by_id_sync(0)
print(json.dumps({"id":t.id,"instance":t.instance_id,"version":t.config_version}),flush=True)
'''
    child_env = os.environ.copy()
    child_env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2]) + os.pathsep + child_env.get("PYTHONPATH", "")
    children = [subprocess.Popen([sys.executable, "-B", "-c", child, str(db)], cwd=tmp_path, env=child_env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(4)]
    results = []
    for proc in children:
        out, err = proc.communicate(timeout=15)
        assert proc.returncode == 0, err
        results.append(json.loads(out))
    assert all(r["id"] == 0 and r["version"] == 1 for r in results)
    assert len({r["instance"] for r in results}) == 1
    repo = SqliteTaskRepository(str(db), None)
    assert repo._delete_sync(0)
    create = '''import asyncio,json,sys
from src.infrastructure.persistence.sqlite_task_repository import SqliteTaskRepository
from src.services.task_service import TaskService
from src.domain.models.task import TaskCreate
s=TaskService(SqliteTaskRepository(sys.argv[1],None))
t=asyncio.run(s.create_task(TaskCreate(task_name="new",keyword="x",decision_mode="keyword",keyword_rules=["x"])))
print(json.dumps({"id":t.id,"instance":t.instance_id}))
'''
    finished = subprocess.run([sys.executable, "-B", "-c", create, str(db)], cwd=tmp_path, env=child_env, text=True, capture_output=True, timeout=15)
    assert finished.returncode == 0, finished.stderr
    replacement = json.loads(finished.stdout)
    assert replacement["id"] == 1 and replacement["instance"] != results[0]["instance"]
