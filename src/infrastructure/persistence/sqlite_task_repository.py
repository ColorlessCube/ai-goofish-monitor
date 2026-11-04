"""SQLite tasks: transactional partial updates and instance/version CAS."""
from __future__ import annotations

import asyncio
import json
from uuid import uuid4
from fastapi import HTTPException

from src.domain.models.task import Task, TaskCreate, TaskUpdate
from src.domain.repositories.task_repository import TaskRepository
from src.infrastructure.persistence.sqlite_bootstrap import bootstrap_sqlite_storage
from src.infrastructure.persistence.sqlite_connection import sqlite_connection
from src.services import prompt_assets as assets


def _row_to_task(row) -> Task:
    payload = dict(row)
    for field in ("enabled", "analyze_images", "personal_only", "free_shipping", "is_running"):
        payload[field] = bool(payload[field])
    payload["keyword_rules"] = json.loads(payload.pop("keyword_rules_json") or "[]")
    return Task(**payload)


def find_task_by_name_sync(task_name: str) -> Task | None:
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        rows = conn.execute("SELECT * FROM tasks WHERE task_name = ? ORDER BY id", (task_name,)).fetchall()
    # Legacy duplicates must never silently select the first task.
    return _row_to_task(rows[0]) if len(rows) == 1 else None


def find_task_by_id_sync(task_id: int, instance_id: str | None = None) -> Task | None:
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    task = _row_to_task(row) if row else None
    return task if task and (instance_id is None or task.instance_id == instance_id) else None


class SqliteTaskRepository(TaskRepository):
    def __init__(self, db_path=None, legacy_config_file="config.json", prompts_dir=None):
        self.db_path = db_path
        self.legacy_config_file = legacy_config_file
        self.prompts_dir = prompts_dir

    def _bootstrap(self):
        bootstrap_sqlite_storage(self.db_path, legacy_config_file=self.legacy_config_file)

    async def find_all(self):
        return await asyncio.to_thread(self._find_all_sync)

    async def find_by_id(self, task_id):
        return await asyncio.to_thread(self._find_by_id_sync, task_id)

    async def save(self, task, *, on_saved=None):
        return await asyncio.to_thread(self._save_sync, task, on_saved)

    async def clone(self, source, *, task_name, enabled, guard):
        return await asyncio.to_thread(self._clone_sync, source, task_name, enabled, guard)

    async def patch(self, task_id, update, *, expected=None, guard=None):
        return await asyncio.to_thread(self._patch_sync, task_id, update, expected, guard)

    async def delete(self, task_id, *, expected=None, guard=None):
        return await asyncio.to_thread(self._delete_sync, task_id, expected, guard)

    def _find_all_sync(self):
        self._bootstrap()
        with sqlite_connection(self.db_path) as conn:
            rows = conn.execute("SELECT * FROM tasks ORDER BY id").fetchall()
        return [_row_to_task(row) for row in rows]

    def _find_by_id_sync(self, task_id):
        self._bootstrap()
        with sqlite_connection(self.db_path) as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return _row_to_task(row) if row else None

    @staticmethod
    def _check(current, expected, guard):
        if expected is not None and (current is None or (current.instance_id, current.config_version) != (expected.instance_id, expected.config_version)):
            raise HTTPException(409, "revision_conflict")
        if current is None:
            raise HTTPException(404, "task_not_found")
        if guard:
            guard(current)

    @staticmethod
    def _unique(conn, task, previous=None):
        # Existing duplicate names survive migration but cannot be newly assigned.
        if previous is not None and task.task_name == previous.task_name:
            return
        if conn.execute("SELECT 1 FROM tasks WHERE task_name=? AND id != ?", (task.task_name, task.id if task.id is not None else -1)).fetchone():
            raise HTTPException(409, "task_name_in_use")

    def _save_sync(self, task, on_saved=None):
        if task.id is not None:
            return self._patch_sync(task.id, TaskUpdate.model_validate(task.model_dump()), task, None)
        self._bootstrap()
        with assets.asset_lock(self.prompts_dir), sqlite_connection(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._unique(conn, task)
            assets.validate_references(task, root=self.prompts_dir)
            task = self._insert(conn, task)
            if on_saved is not None:
                on_saved(conn, task)
            conn.commit()
        return task

    def _insert(self, conn, task):
        task = task.model_copy(update={"id": self._next_task_id(conn), "instance_id": uuid4().hex, "config_version": 1})
        values = self._task_values(task)
        columns = ",".join(values)
        placeholders = ",".join(":" + key for key in values)
        conn.execute(f"INSERT INTO tasks ({columns}) VALUES ({placeholders})", values)
        return task

    def _clone_sync(self, source, task_name, enabled, guard):
        self._bootstrap()
        filename = None
        try:
            with assets.asset_lock(self.prompts_dir) as fd, sqlite_connection(self.db_path) as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT * FROM tasks WHERE id=?", (source.id,)).fetchone()
                current = _row_to_task(row) if row else None
                self._check(current, source, guard)
                config = TaskCreate.model_validate(current.model_dump() | {"task_name": task_name, "enabled": enabled})
                task = Task(**config.model_dump(), is_running=False)
                self._unique(conn, task)
                if task.decision_mode == "ai":
                    content = assets.read_at(fd, task.ai_prompt_criteria_file)
                    filename = "prompts/clone_" + uuid4().hex + "_criteria.txt"
                    assets.write_at(fd, filename, content, create=True)
                    task.ai_prompt_criteria_file = filename
                else:
                    task.ai_prompt_criteria_file = ""
                assets.validate_references(task, root=self.prompts_dir)
                task = self._insert(conn, task)
                conn.commit()
            return task
        except Exception:
            if filename is not None:
                assets.cleanup_unreferenced(self, filename, self.prompts_dir)
            raise

    def _patch_sync(self, task_id, update, expected=None, guard=None):
        self._bootstrap()
        with assets.asset_lock(self.prompts_dir), sqlite_connection(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            current = _row_to_task(row) if row else None
            self._check(current, expected, guard)
            changes = update.model_dump(exclude_unset=True)
            updated = current.apply_update(update)
            self._unique(conn, updated, current)
            assets.validate_references(updated, current, root=self.prompts_dir, changed_fields=changes)
            config_changed = any(key != "is_running" and getattr(updated, key) != getattr(current, key) for key in changes)
            updated.config_version = current.config_version + int(config_changed)
            values = self._task_values(updated)
            values["expected_instance"] = current.instance_id
            values["expected_version"] = current.config_version
            assignments = ",".join(f"{key}=:{key}" for key in self._task_values(updated) if key not in {"id", "instance_id"})
            cursor = conn.execute(f"UPDATE tasks SET {assignments} WHERE id=:id AND instance_id=:expected_instance AND config_version=:expected_version", values)
            if cursor.rowcount != 1:
                raise HTTPException(409, "revision_conflict")
            conn.commit()
        return updated

    def _delete_sync(self, task_id, expected=None, guard=None):
        self._bootstrap()
        with assets.asset_lock(self.prompts_dir), sqlite_connection(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None and expected is None and guard is None:
                return False
            current = _row_to_task(row) if row else None
            self._check(current, expected, guard)
            cursor = conn.execute("DELETE FROM tasks WHERE id=? AND instance_id=? AND config_version=?", (task_id, current.instance_id, current.config_version))
            conn.commit()
        return cursor.rowcount == 1

    def _next_task_id(self, conn):
        return int(conn.execute("SELECT value FROM app_metadata WHERE key='tasks:next_id'").fetchone()[0])

    @staticmethod
    def _task_values(task):
        values = task.model_dump()
        for field in ("enabled", "analyze_images", "personal_only", "free_shipping", "is_running"):
            values[field] = int(values[field])
        values["keyword_rules_json"] = json.dumps(values.pop("keyword_rules") or [], ensure_ascii=False)
        return values
