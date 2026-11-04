"""Durable native generation outcomes; never replay jobs after restart."""
import asyncio
import json
from src.infrastructure.persistence.sqlite_connection import sqlite_connection


async def settle(awaitable):
    """Do not inspect a receipt while a cancelled to_thread write still commits."""
    pending = asyncio.ensure_future(awaitable)
    cancelled = None
    while not pending.done():
        try:
            await asyncio.shield(pending)
        except asyncio.CancelledError as exc:
            cancelled = exc
        except BaseException:
            break
    if cancelled is not None:
        # Consume the eventual error, but preserve cancellation for the worker's
        # durable reconciliation. Repeated cancel() must not cancel the DB waiter.
        if not pending.cancelled():
            pending.exception()
        raise cancelled
    return pending.result()


class GenerationReceipts:
    def __init__(self, repository):
        self.repository = repository

    def _prepare(self):
        self.repository._bootstrap()
        with sqlite_connection(self.repository.db_path) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS mcp_generation_jobs (job_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            conn.commit()

    async def create(self, job):
        def write():
            self._prepare()
            data = {"job_id": job.job_id, "task_name": job.task_name, "status": "queued", "current_step": None, "error": None}
            with sqlite_connection(self.repository.db_path) as conn:
                conn.execute("INSERT INTO mcp_generation_jobs VALUES (?,?)", (job.job_id, json.dumps(data)))
                conn.commit()
        await asyncio.to_thread(write)

    async def get(self, job_id):
        def read():
            self._prepare()
            with sqlite_connection(self.repository.db_path) as conn:
                row = conn.execute("SELECT payload FROM mcp_generation_jobs WHERE job_id=?", (job_id,)).fetchone()
            return json.loads(row[0]) if row else None
        return await asyncio.to_thread(read)

    @staticmethod
    def change_in_transaction(conn, job_id, changes):
        row = conn.execute("SELECT payload FROM mcp_generation_jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise RuntimeError("generation receipt missing")
        data = json.loads(row[0])
        if data["status"] == "completed":
            return data
        if data.get("task_committed"):
            changes = dict(changes)
            changes.pop("task", None)
            changes.pop("task_committed", None)
            if changes.get("status") == "failed":
                changes.update(status="partial_success", error="scheduler_sync_required", retry_safe=False)
        data = data | changes
        conn.execute("UPDATE mcp_generation_jobs SET payload=? WHERE job_id=?", (json.dumps(data), job_id))
        return data

    async def change(self, job_id, **changes):
        def write():
            with sqlite_connection(self.repository.db_path) as conn:
                conn.execute("BEGIN IMMEDIATE")
                data = self.change_in_transaction(conn, job_id, changes)
                conn.commit()
                return data
        return await settle(asyncio.to_thread(write))

    async def recover(self, job_id, generation, *, uncertain=False):
        """Use the creation transaction's snapshot even if save() never returned."""
        try:
            data = await self.get(job_id)
        except Exception:
            data = None
        if data is None:
            live = await generation.get_job(job_id)
            data = live.model_dump(mode="json") if live else {"job_id": job_id}
            if data.get("task_committed") is None:
                data.pop("task_committed", None)
            data.update(status="unknown", retry_safe=False, error="generation_outcome_unknown")
            await generation.publish(job_id, data)
            return data
        if data["status"] not in {"completed", "partial_success", "failed", "unknown"}:
            if data.get("task_committed"):
                changes = dict(status="partial_success", current_step="scheduler",
                               error="scheduler_sync_required", retry_safe=False)
            elif uncertain:
                changes = dict(status="unknown", error="generation_outcome_unknown", retry_safe=False)
            else:
                changes = dict(status="failed", error="generation_failed", task_committed=False, retry_safe=False)
            try:
                data = await self.change(job_id, **changes)
            except Exception:
                data = data | {"status": "unknown", "error": "generation_outcome_unknown", "retry_safe": False}
        await generation.publish(job_id, data)
        return data

    async def observe(self, job_id, generation):
        try:
            data = await self.get(job_id)
        except Exception:
            data = None
        if data is None or (data["status"] in {"queued", "running"} and not generation.is_active(job_id)):
            return await self.recover(job_id, generation, uncertain=True)
        if data["status"] not in {"queued", "running"}:
            return data | {"retry_safe": False}
        live = await generation.get_job(job_id)
        # Progress is advisory. Status and committed snapshot are authoritative.
        progress = live.model_dump(mode="json") if live else {}
        if progress.get("task_committed") is None:
            progress.pop("task_committed", None)
        return progress | data | {"retry_safe": False}
