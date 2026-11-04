"""Synchronous keyword generation: real fixture transactions, no external I/O."""
import asyncio
import json
import socket
import sqlite3
import threading

import httpx
import pytest

from src.api import dependencies as deps
from src.api.routes import tasks as task_routes
from src.domain.models.task import TaskCreate, TaskUpdate
from src.services import prompt_assets as assets
from src.services import task_generation_runner as runner
from src.services.generation_receipts import GenerationReceipts
from src.services.task_generation_service import TaskGenerationService
from src.services.task_payloads import serialize_task


@pytest.fixture(autouse=True)
def prohibit_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("NETWORK FORBIDDEN IN KEYWORD RECEIPT TESTS")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(task_routes, "generate_criteria", blocked)
    monkeypatch.setattr(runner, "generate_criteria", blocked)
    monkeypatch.setattr(TaskGenerationService, "track", blocked)


def only_receipt(ctx):
    with sqlite3.connect(ctx["db_path"]) as conn:
        rows = conn.execute("SELECT payload FROM mcp_generation_jobs").fetchall()
    assert len(rows) == 1
    return json.loads(rows[0][0])


def service(ctx):
    return ctx["app"].dependency_overrides[deps.get_task_service]()


async def generate(ctx):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=ctx["app"]), base_url="http://fixture"
    ) as client:
        return await client.post("/api/tasks/generate", json={
            "task_name": "sync keyword postcommit", "keyword": "camera",
            "decision_mode": "keyword", "keyword_rules": ["working"],
            "cron": "0 * * * *",
        })


@pytest.mark.asyncio
async def test_rest_keyword_commit_unlock_failure_preserves_committed_outcome(api_context, monkeypatch):
    svc = service(api_context)
    original_flock = assets.fcntl.flock
    injected = []

    def fail_postcommit_unlock(fd, operation):
        result = original_flock(fd, operation)
        if operation == assets.fcntl.LOCK_UN and not injected:
            with sqlite3.connect(svc.repository.db_path) as conn:
                count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            if count:
                injected.append(True)
                raise OSError("fixture unlock failure after real SQLite COMMIT")
        return result

    monkeypatch.setattr(assets.fcntl, "flock", fail_postcommit_unlock)
    response = await generate(api_context)
    saved = await svc.get_all_tasks()
    assert injected and len(saved) == 1
    assert response.json().get("task_committed") is True
    assert response.json().get("task", {}).get("instance_id") == saved[0].instance_id
    assert response.json().get("status") == "partial_success"
    assert response.status_code == 200
    assert response.json()["retry_safe"] is False
    assert response.json()["error"] == "scheduler_sync_required"
    assert response.json()["task"]["enabled"] is True
    assert api_context["scheduler_service"].reload_calls == 0
    assert api_context["process_service"].started == []
    assert only_receipt(api_context) == response.json()


@pytest.mark.asyncio
async def test_sync_keyword_normal_response_and_durable_snapshot_are_compatible(api_context):
    response = await generate(api_context)
    svc = service(api_context)
    saved, = await svc.get_all_tasks()
    assert response.status_code == 200
    assert response.json() == {
        "message": "任务创建成功。",
        "task": serialize_task(saved, api_context["scheduler_service"]),
    }
    assert saved.enabled is True and saved.ai_prompt_criteria_file == ""
    assert response.json()["task"]["next_run_at"]
    assert api_context["scheduler_service"].reload_calls == 1
    assert api_context["process_service"].started == []
    receipt = only_receipt(api_context)
    assert receipt["status"] == "completed"
    assert receipt["task_committed"] is True and receipt["retry_safe"] is False
    assert receipt["task"] == saved.model_dump(mode="json")
    await svc.update_task(saved.id, TaskUpdate(keyword="later edit"))
    restored = await GenerationReceipts(svc.repository).observe(receipt["job_id"], TaskGenerationService())
    assert restored == receipt
    assert not api_context["task_generation_service"].is_active(receipt["job_id"])


@pytest.mark.asyncio
@pytest.mark.parametrize("race", ["unchanged", "edit", "replace"])
async def test_sync_keyword_scheduler_failure_preserves_creation_snapshot(api_context, monkeypatch, race):
    svc = service(api_context)
    original = []

    async def failed_scheduler(tasks):
        created, = tasks
        original.append(created.model_dump(mode="json"))
        if race == "edit":
            await svc.update_task(created.id, TaskUpdate(keyword="later edit"))
        elif race == "replace":
            await svc.delete_task(created.id)
            await svc.create_task(TaskCreate(task_name=created.task_name, keyword="replacement",
                decision_mode="keyword", keyword_rules=["replacement"]))
        raise OSError("fixture scheduler unavailable")

    monkeypatch.setattr(api_context["scheduler_service"], "reload_jobs", failed_scheduler)
    response = await generate(api_context)
    outcome = response.json()
    assert response.status_code == 200
    assert outcome["status"] == "partial_success"
    assert outcome["task_committed"] is True and outcome["retry_safe"] is False
    assert outcome["error"] == "scheduler_sync_required"
    assert len(original) == 1 and outcome["task"] == original[0]
    assert only_receipt(api_context) == outcome
    restarted = await GenerationReceipts(svc.repository).observe(outcome["job_id"], TaskGenerationService())
    assert restarted == outcome


@pytest.mark.asyncio
async def test_sync_keyword_real_rollback_reverts_task_and_commit_receipt(api_context, monkeypatch):
    original_change = GenerationReceipts.change_in_transaction
    attempted = []

    def fail_after_receipt_write(conn, job_id, changes):
        result = original_change(conn, job_id, changes)
        if changes.get("task_committed") is True:
            assert conn.in_transaction
            assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
            assert result["task_committed"] is True
            attempted.append(result)
            raise RuntimeError("fixture failure before real COMMIT")
        return result

    monkeypatch.setattr(GenerationReceipts, "change_in_transaction", staticmethod(fail_after_receipt_write))
    response = await generate(api_context)
    outcome = response.json()
    assert response.status_code == 500
    assert len(attempted) == 1
    assert await service(api_context).get_all_tasks() == []
    assert outcome["status"] == "failed" and outcome["task_committed"] is False
    assert outcome["retry_safe"] is False and not outcome.get("task")
    assert only_receipt(api_context) == outcome
    assert api_context["scheduler_service"].reload_calls == 0


@pytest.mark.asyncio
async def test_sync_keyword_existing_name_is_not_proof_of_this_commit(api_context):
    svc = service(api_context)
    existing = await svc.create_task(TaskCreate(task_name="sync keyword postcommit", keyword="existing",
        decision_mode="keyword", keyword_rules=["existing"]))
    response = await generate(api_context)
    assert response.status_code == 409
    outcome = response.json()
    assert outcome["detail"] == "task_name_in_use"
    assert outcome["status"] == "failed" and outcome["task_committed"] is False
    assert outcome["retry_safe"] is False and not outcome.get("task")
    assert [t.model_dump() for t in await svc.get_all_tasks()] == [existing.model_dump()]
    assert api_context["scheduler_service"].reload_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("committed", [False, True])
async def test_sync_keyword_unreadable_evidence_is_unknown_not_inferred(api_context, monkeypatch, committed):
    async def unreadable(self, job_id):
        raise OSError("fixture receipt read unavailable")

    async def scheduler_failed(tasks):
        raise RuntimeError("fixture postcommit scheduler failure")

    def fail_before_insert(conn, task):
        raise RuntimeError("fixture precommit insertion failure")

    monkeypatch.setattr(GenerationReceipts, "get", unreadable)
    if committed:
        monkeypatch.setattr(api_context["scheduler_service"], "reload_jobs", scheduler_failed)
    else:
        monkeypatch.setattr(service(api_context).repository, "_insert", fail_before_insert)
    response = await generate(api_context)
    outcome = response.json()
    assert response.status_code == 200
    assert outcome["status"] == "unknown" and outcome["retry_safe"] is False
    assert outcome["error"] == "generation_outcome_unknown"
    assert "task_committed" not in outcome and not outcome.get("task")
    saved = await service(api_context).get_all_tasks()
    assert len(saved) == int(committed)
    durable = only_receipt(api_context)
    if committed:
        assert durable["task_committed"] is True
        assert durable["task"]["instance_id"] == saved[0].instance_id
    else:
        assert "task_committed" not in durable


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_count", [1, 2])
@pytest.mark.parametrize("rollback", [False, True])
async def test_sync_keyword_cancellation_settles_inflight_creation(api_context, monkeypatch, cancel_count, rollback):
    svc = service(api_context)
    generation = api_context["task_generation_service"]
    reached, release = threading.Event(), threading.Event()
    original_save = svc.repository._save_sync

    def delayed_save(*args, **kwargs):
        reached.set()
        assert release.wait(5)
        return original_save(*args, **kwargs)

    def fail_before_insert(conn, task):
        raise RuntimeError("fixture transaction rollback after cancellation")

    monkeypatch.setattr(svc.repository, "_save_sync", delayed_save)
    if rollback:
        monkeypatch.setattr(svc.repository, "_insert", fail_before_insert)
    request = asyncio.create_task(generate(api_context))
    try:
        assert await asyncio.to_thread(reached.wait, 2)
        receipt = only_receipt(api_context)
        assert generation.is_active(receipt["job_id"])
        for _ in range(cancel_count):
            request.cancel()
            await asyncio.sleep(.01)
        assert not request.done(), "Do not abandon an in-flight SQLite writer"
        pending = await GenerationReceipts(svc.repository).observe(receipt["job_id"], generation)
        assert pending["status"] in {"queued", "running"}
        assert "task_committed" not in pending
        assert pending["retry_safe"] is False
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await request
    outcome = await GenerationReceipts(svc.repository).observe(receipt["job_id"], TaskGenerationService())
    saved = await svc.get_all_tasks()
    if rollback:
        assert outcome["status"] == "unknown"
        assert "task_committed" not in outcome and not outcome.get("task")
        assert saved == []
    else:
        assert len(saved) == 1
        assert outcome["status"] == "partial_success"
        assert outcome["task_committed"] is True
        assert outcome["task"]["instance_id"] == saved[0].instance_id
        assert outcome["error"] == "scheduler_sync_required"
    assert outcome["retry_safe"] is False
    assert not generation.is_active(receipt["job_id"])
    assert api_context["scheduler_service"].reload_calls == 0


@pytest.mark.asyncio
async def test_sync_keyword_terminal_persistence_failure_is_partial(api_context, monkeypatch):
    original_change = GenerationReceipts.change

    async def fail_completed(self, job_id, **changes):
        if changes.get("status") == "completed":
            raise OSError("fixture terminal persistence unavailable")
        return await original_change(self, job_id, **changes)

    monkeypatch.setattr(GenerationReceipts, "change", fail_completed)
    response = await generate(api_context)
    outcome = response.json()
    assert outcome["status"] == "partial_success"
    assert outcome["task_committed"] is True and outcome["retry_safe"] is False
    assert only_receipt(api_context) == outcome
    assert api_context["scheduler_service"].reload_calls == 1


@pytest.mark.asyncio
async def test_sync_keyword_completed_observation_survives_request_cancellation(api_context, monkeypatch):
    svc = service(api_context)
    generation = api_context["task_generation_service"]
    reached, hold = asyncio.Event(), asyncio.Event()
    original_publish = generation.publish

    async def paused_publish(job_id, data):
        result = await original_publish(job_id, data)
        if data["status"] == "completed":
            reached.set()
            await hold.wait()
        return result

    monkeypatch.setattr(generation, "publish", paused_publish)
    request = asyncio.create_task(generate(api_context))
    try:
        await asyncio.wait_for(reached.wait(), 2)
        receipt = only_receipt(api_context)
        before = await GenerationReceipts(svc.repository).observe(receipt["job_id"], generation)
        monkeypatch.setattr(generation, "publish", original_publish)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
    finally:
        hold.set()
        if not request.done():
            await request
    after = await GenerationReceipts(svc.repository).observe(receipt["job_id"], TaskGenerationService())
    assert before["status"] == after["status"] == "completed"
    assert before["task"] == after["task"]
    assert after["task_committed"] is True and after["retry_safe"] is False
    assert not generation.is_active(receipt["job_id"])
