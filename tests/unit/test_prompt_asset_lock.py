"""Shared asset locking is process-wide, not merely an asyncio/MCP mutex."""
import asyncio
import os
from pathlib import Path
import subprocess
import sys

import pytest
from fastapi import HTTPException

from src.services import prompt_assets as assets
from src.api import dependencies as deps
from src.domain.models.task import TaskCreate, TaskUpdate
from src.infrastructure.persistence.sqlite_task_repository import SqliteTaskRepository
from src.services.task_service import TaskService
from test_mcp_task_tools import mcp_context, invoke, task_payload


def test_cross_process_reference_writer_waits_and_rechecks_deleted_asset(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "prompts"
    root.mkdir()
    (root / "base.txt").write_text("base")
    (root / "criteria.txt").write_text("criteria")
    repo = SqliteTaskRepository(str(tmp_path / "db.sqlite"), None)
    asyncio.run(repo.find_all())
    child = '''import asyncio, sys
from fastapi import HTTPException
from src.domain.models.task import TaskCreate
from src.infrastructure.persistence.sqlite_task_repository import SqliteTaskRepository
from src.services.task_service import TaskService
print("attempting", flush=True)
try:
    asyncio.run(TaskService(SqliteTaskRepository(sys.argv[1], None)).create_task(TaskCreate(task_name="child", keyword="camera", description="criteria", ai_prompt_base_file="prompts/base.txt", ai_prompt_criteria_file="prompts/criteria.txt")))
    print("saved", flush=True)
except HTTPException as exc:
    print(exc.detail, flush=True)
'''
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2]))
    with assets.asset_lock(root) as fd:
        process = subprocess.Popen([sys.executable, "-c", child, str(tmp_path / "db.sqlite")], cwd=tmp_path, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        assert process.stdout.readline().strip() == "attempting"
        try:
            with pytest.raises(subprocess.TimeoutExpired):
                process.wait(timeout=0.15)
            os.unlink("criteria.txt", dir_fd=fd)
        except BaseException:
            process.kill()
            process.communicate(timeout=5)
            raise
    output, errors = process.communicate(timeout=10)
    assert process.returncode == 0, errors
    assert "prompt_not_found" in output and "saved" not in output
    assert asyncio.run(repo.find_all()) == []


def test_descriptor_open_rejects_leaf_swapped_to_symlink(tmp_path, monkeypatch):
    root = tmp_path / "prompts"
    root.mkdir()
    leaf = root / "criteria.txt"
    leaf.write_text("safe")
    secret = tmp_path / "outside.txt"
    secret.write_text("private")
    original = assets.os.open
    def swap(path, flags, *args, **kwargs):
        if path == "criteria.txt":
            leaf.unlink()
            leaf.symlink_to(secret)
        return original(path, flags, *args, **kwargs)
    monkeypatch.setattr(assets.os, "open", swap)
    with pytest.raises(HTTPException, match="unsafe_prompt_path"):
        assets.read_prompt("criteria.txt", root)


@pytest.mark.asyncio
async def test_failure_cleanup_does_not_delete_newly_adopted_asset(mcp_context, monkeypatch):
    service = mcp_context["app"].dependency_overrides[deps.get_task_service]()
    original = await invoke("create_task", task_payload(), mcp_context)
    adopted = []
    async def fail_after_adoption(*args, **kwargs):
        filename = next(Path("prompts").glob("task_0_*_criteria.txt"))
        adopted.append(filename)
        await service.create_task(TaskCreate(**task_payload(task_name="adopter", ai_prompt_criteria_file=str(filename))))
        raise RuntimeError("original owner save failed")
    monkeypatch.setattr(service, "update_task", fail_after_adoption)
    with pytest.raises(RuntimeError):
        await invoke("update_task", {"task_id": 0, "expected_revision": original["revision"], "changes": {"description": "new"}, "criteria_content": "new"}, mcp_context)
    assert adopted[0].exists(), "Cleanup must recheck all references under the shared lock"


@pytest.mark.asyncio
async def test_mode_switch_and_reenable_validate_unchanged_assets(mcp_context):
    service = mcp_context["app"].dependency_overrides[deps.get_task_service]()
    original = await invoke("create_task", task_payload(enabled=False), mcp_context)
    Path("prompts/criteria.txt").unlink()
    with pytest.raises(HTTPException, match="prompt_not_found"):
        await service.update_task(0, TaskUpdate(enabled=True))
    task = await service.create_task(TaskCreate(task_name="keyword", keyword="camera", decision_mode="keyword", keyword_rules=["ok"], ai_prompt_criteria_file="prompts/missing.txt"))
    with pytest.raises(HTTPException, match="prompt_not_found"):
        await service.update_task(task.id, TaskUpdate(decision_mode="ai", description="screening"))
