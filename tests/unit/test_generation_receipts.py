"""Generation receipts are durable, immutable snapshots, never live-ID lookups."""
import pytest
from src.api import dependencies as deps
from src.services.task_generation_service import TaskGenerationService
from test_mcp_task_tools import mcp_context, invoke


@pytest.mark.asyncio
async def test_job_survives_service_restart_without_auto_reexecution(mcp_context, api_context, monkeypatch):
    queue = []
    monkeypatch.setattr(api_context["task_generation_service"], "track", lambda coroutine, *, job_id: queue.append(coroutine))
    accepted = await invoke("generate_task", {"task_name": "generated", "keyword": "camera", "decision_mode": "keyword", "keyword_rules": ["working"]}, mcp_context)
    await queue.pop()
    job_id = accepted["job"]["job_id"]
    before = (await invoke("get_generation_job", {"job_id": job_id}, mcp_context))["job"]
    mcp_context["app"].dependency_overrides[deps.get_task_generation_service] = lambda: TaskGenerationService()
    after = (await invoke("get_generation_job", {"job_id": job_id}, mcp_context))["job"]
    assert after == before and after["task"]["instance_id"]


@pytest.mark.asyncio
async def test_interrupted_pending_job_is_unknown_not_not_found_or_auto_retry(mcp_context, api_context, monkeypatch):
    queue = []
    monkeypatch.setattr(api_context["task_generation_service"], "track", lambda coroutine, *, job_id: queue.append(coroutine))
    accepted = await invoke("generate_task", {"task_name": "queued", "keyword": "camera", "decision_mode": "keyword", "keyword_rules": ["working"]}, mcp_context)
    queue.pop().close()
    mcp_context["app"].dependency_overrides[deps.get_task_generation_service] = lambda: TaskGenerationService()
    after = (await invoke("get_generation_job", {"job_id": accepted["job"]["job_id"]}, mcp_context))["job"]
    assert after["status"] == "unknown" and after["retry_safe"] is False
