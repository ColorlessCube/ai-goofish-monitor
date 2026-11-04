"""REST shares assets/CAS with MCP; generation is stubbed."""
import asyncio
from pathlib import Path
import pytest
import httpx
from src.api import dependencies as deps
from src.api.routes import tasks
from src.services import task_generation_runner as runner
from src.domain.models.task import TaskGenerateRequest
from test_mcp_task_tools import mcp_context, invoke, task_payload


@pytest.mark.asyncio
async def test_rest_description_edit_never_overwrites_shared_keyword_criteria(mcp_context, monkeypatch):
    await invoke("create_task", task_payload(ai_prompt_criteria_file="prompts/criteria.txt"), mcp_context)
    legacy = Path("prompts/sony_a7m4_criteria.txt")
    legacy.write_text("shared sibling criteria")
    sibling = await invoke("create_task", task_payload(task_name="sibling", ai_prompt_criteria_file=str(legacy)), mcp_context)
    async def generated(**kwargs):
        return "new isolated criteria"
    monkeypatch.setattr(tasks, "generate_criteria", generated)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=mcp_context["app"]), base_url="http://test") as client:
        changed = await client.patch("/api/tasks/0", json={"description": "new requirement"})
    assert changed.status_code == 200, changed.text
    assert legacy.read_text() == "shared sibling criteria"
    assert changed.json()["task"]["ai_prompt_criteria_file"] != sibling["task"]["ai_prompt_criteria_file"]


@pytest.mark.asyncio
async def test_rest_delete_preserves_history_logs_and_rejects_purge(mcp_context, api_context, monkeypatch):
    import sqlite3
    from src.infrastructure.persistence.sqlite_bootstrap import _insert_result_record
    monkeypatch.setenv("APP_DATABASE_FILE", str(api_context["db_path"]))
    await invoke("create_task", task_payload(), mcp_context)
    Path("logs").mkdir()
    Path("logs/camera_0.log").write_text("history")
    with sqlite3.connect(api_context["db_path"]) as conn:
        _insert_result_record(conn, {"任务名称": "camera", "商品信息": {"商品ID": "1"}}, keyword="sony a7m4", filename="sony_a7m4_full_data.jsonl")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=mcp_context["app"]), base_url="http://test") as client:
        refused = await client.delete("/api/tasks/0?purge=true")
        assert refused.status_code == 409
        deleted = await client.delete("/api/tasks/0")
        assert deleted.status_code == 200
    assert Path("logs/camera_0.log").read_text() == "history"
    assert Path("prompts/criteria.txt").exists()
    with sqlite3.connect(api_context["db_path"]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM result_items").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_rest_generation_postcommit_scheduler_failure_keeps_created_asset(mcp_context, api_context, monkeypatch):
    generation = api_context["task_generation_service"]
    job = await generation.create_job("REST generated")
    async def generated(**kwargs):
        return "criteria"
    async def failed(*args):
        raise RuntimeError("scheduler offline")
    monkeypatch.setattr(runner, "generate_criteria", generated)
    monkeypatch.setattr(api_context["scheduler_service"], "reload_jobs", failed)
    service = mcp_context["app"].dependency_overrides[deps.get_task_service]()
    await runner.run_ai_generation_job(job_id=job.job_id, req=TaskGenerateRequest(task_name="REST generated", keyword="camera", description="working"), task_service=service, scheduler_service=api_context["scheduler_service"], generation_service=generation)
    created = await service.get_all_tasks()
    assert len(created) == 1
    assert Path(created[0].ai_prompt_criteria_file).exists()
    result = await generation.get_job(job.job_id)
    assert result.task.instance_id == created[0].instance_id
    assert result.status == "partial_success"
    # The created instance/snapshot must still be observable after process restart.
    from src.services.task_generation_service import TaskGenerationService
    mcp_context["app"].dependency_overrides[deps.get_task_generation_service] = lambda: TaskGenerationService()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=mcp_context["app"]), base_url="http://test") as client:
        response = await client.get(f"/api/tasks/generate-jobs/{job.job_id}")
    assert response.status_code == 200
    recovered = response.json()["job"]
    assert recovered["status"] == "partial_success"
    assert recovered["task"]["instance_id"] == created[0].instance_id
