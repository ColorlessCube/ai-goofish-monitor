"""Storage guarantees use fixture SQLite only, including legacy schema migration."""
import asyncio
import sqlite3

import pytest
from fastapi import HTTPException

from src.domain.models.task import TaskCreate, TaskUpdate
from src.infrastructure.persistence.sqlite_task_repository import SqliteTaskRepository
from src.services.task_service import TaskService


def config(name="task"):
    return TaskCreate(task_name=name, keyword="camera", decision_mode="keyword", keyword_rules=["working"])


@pytest.mark.asyncio
async def test_repository_stale_save_is_cas_and_runtime_does_not_lose_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    repo = SqliteTaskRepository(str(tmp_path / "db.sqlite"), None)
    service = TaskService(repo)
    old = await service.create_task(config())
    current = await service.update_task(old.id, TaskUpdate(min_price="200"))
    with pytest.raises(HTTPException, match="revision_conflict"):
        await repo.save(old.model_copy(update={"max_price": "500"}))
    await service.update_task_status(old.id, True)
    final = await service.get_task(old.id)
    assert final.min_price == "200" and final.max_price is None
    assert final.config_version == current.config_version
    assert final.instance_id == old.instance_id


@pytest.mark.asyncio
async def test_concurrent_partial_rest_writers_merge_in_transaction(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    service = TaskService(SqliteTaskRepository(str(tmp_path / "db.sqlite"), None))
    task = await service.create_task(config())
    await asyncio.gather(service.update_task(task.id, TaskUpdate(min_price="20")), service.update_task(task.id, TaskUpdate(max_price="50")))
    task = await service.get_task(task.id)
    assert (task.min_price, task.max_price) == ("20", "50")
    assert task.config_version == 3


@pytest.mark.asyncio
async def test_old_schema_zero_id_migration_is_stable_and_never_reuses_id(tmp_path, monkeypatch):
    from src.infrastructure.persistence.sqlite_connection import SCHEMA_STATEMENTS
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "old.sqlite"
    # Frozen pre-CAS tasks schema, not the current migration implementation.
    with sqlite3.connect(db) as conn:
        conn.execute('''CREATE TABLE tasks (
          id INTEGER PRIMARY KEY, task_name TEXT NOT NULL, enabled INTEGER NOT NULL,
          keyword TEXT NOT NULL, description TEXT, analyze_images INTEGER NOT NULL,
          max_pages INTEGER NOT NULL, personal_only INTEGER NOT NULL, min_price TEXT,
          max_price TEXT, cron TEXT, ai_prompt_base_file TEXT NOT NULL,
          ai_prompt_criteria_file TEXT NOT NULL, account_state_file TEXT,
          account_strategy TEXT NOT NULL, free_shipping INTEGER NOT NULL,
          new_publish_option TEXT, region TEXT, decision_mode TEXT NOT NULL,
          keyword_rules_json TEXT NOT NULL, is_running INTEGER NOT NULL)''')
        conn.execute("INSERT INTO tasks VALUES (0,'legacy',0,'camera','desc',1,1,1,NULL,NULL,NULL,'prompts/base_prompt.txt','',NULL,'auto',1,NULL,NULL,'keyword','[\"working\"]',0)")
    repo = SqliteTaskRepository(str(db), None)
    old = await repo.find_by_id(0)
    assert old.id == 0 and old.instance_id and old.config_version >= 1
    assert (await SqliteTaskRepository(str(db), None).find_by_id(0)).instance_id == old.instance_id
    await repo.delete(0)
    new = await TaskService(repo).create_task(config())
    assert new.id > 0 and new.instance_id != old.instance_id


@pytest.mark.asyncio
async def test_all_new_writers_reject_duplicate_names(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    service = TaskService(SqliteTaskRepository(str(tmp_path / "db.sqlite"), None))
    await service.create_task(config())
    with pytest.raises(HTTPException, match="task_name_in_use"):
        await service.create_task(config())
    second = await service.create_task(config("second"))
    with pytest.raises(HTTPException, match="task_name_in_use"):
        await service.update_task(second.id, TaskUpdate(task_name="task"))
