"""Receipt failures and worker lifetime edges; fixture files/SQLite only."""
import asyncio
import threading
from pathlib import Path

import pytest

from test_runtime_receipt_review import (
    mcp_context, service, prohibit_network, runner, ops, TaskGenerateRequest,
    GenerationReceipts, task_routes, TaskGenerationService,
)


async def job_worker(ctx, api_context, monkeypatch, entrypoint):
    generation = api_context['task_generation_service']
    svc = service(ctx)
    job = await generation.create_job('edge outcome')
    await GenerationReceipts(svc.repository).create(job)

    async def generated(**kwargs):
        return 'isolated generated criteria'

    monkeypatch.setattr(runner, 'generate_criteria', generated)
    monkeypatch.setattr(ops, 'generate_criteria', generated)
    if entrypoint == 'rest':
        coroutine = runner.run_ai_generation_job(job_id=job.job_id,
            req=TaskGenerateRequest(task_name=job.task_name, keyword='camera', description='working'),
            task_service=svc, scheduler_service=api_context['scheduler_service'], generation_service=generation)
    else:
        from src.mcp.task_tools import GenerateTask
        model = GenerateTask(task_name=job.task_name, keyword='camera', description='working')
        coroutine = ops.run_generation(model, ctx, job.job_id, generation)
    return job, coroutine


@pytest.mark.asyncio
@pytest.mark.parametrize('entrypoint', ['rest', 'mcp'])
@pytest.mark.parametrize('cancel_count', [1, 2])
async def test_cancellation_settles_inflight_creation_before_receipt_recovery(mcp_context, api_context, monkeypatch, entrypoint, cancel_count):
    svc = service(mcp_context)
    generation = api_context['task_generation_service']
    reached, release = threading.Event(), threading.Event()
    save = svc.repository._save_sync

    def delayed(*args, **kwargs):
        reached.set()
        assert release.wait(3)
        return save(*args, **kwargs)

    monkeypatch.setattr(svc.repository, '_save_sync', delayed)
    job, coroutine = await job_worker(mcp_context, api_context, monkeypatch, entrypoint)
    worker = asyncio.create_task(coroutine)
    try:
        assert await asyncio.to_thread(reached.wait, 2)
        for _ in range(cancel_count):
            worker.cancel()
            await asyncio.sleep(.01)
        assert not worker.done(), 'Do not abandon a still-committing creation thread'
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await worker
    result = await GenerationReceipts(svc.repository).observe(job.job_id, generation)
    saved = await svc.get_all_tasks()
    assert result['status'] == 'partial_success'
    assert result['task_committed'] is True and result['retry_safe'] is False
    assert result['task']['instance_id'] == saved[0].instance_id
    assert Path(saved[0].ai_prompt_criteria_file).read_text() == 'isolated generated criteria'
    assert not generation.is_active(job.job_id)


@pytest.mark.asyncio
@pytest.mark.parametrize('entrypoint', ['rest', 'mcp'])
async def test_unreadable_postcommit_receipt_is_unknown_without_asset_cleanup(mcp_context, api_context, monkeypatch, entrypoint):
    svc = service(mcp_context)
    generation = api_context['task_generation_service']
    original_get = GenerationReceipts.get
    unavailable = False

    async def failed_scheduler(*args):
        nonlocal unavailable
        unavailable = True
        raise OSError('fixture scheduler failure')

    async def read(self, job_id):
        if unavailable:
            raise OSError('fixture receipt read failure')
        return await original_get(self, job_id)

    monkeypatch.setattr(GenerationReceipts, 'get', read)
    monkeypatch.setattr(api_context['scheduler_service'], 'reload_jobs', failed_scheduler)
    job, coroutine = await job_worker(mcp_context, api_context, monkeypatch, entrypoint)
    await coroutine
    live = await generation.get_job(job.job_id)
    assert live.status == 'unknown' and live.retry_safe is False
    unreadable = await GenerationReceipts(svc.repository).observe(job.job_id, generation)
    assert 'task_committed' not in unreadable or isinstance(unreadable['task_committed'], bool)
    saved = await svc.get_all_tasks()
    assert len(saved) == 1
    assert Path(saved[0].ai_prompt_criteria_file).read_text() == 'isolated generated criteria'
    unavailable = False
    restored = await GenerationReceipts(svc.repository).observe(job.job_id, TaskGenerationService())
    assert restored['status'] == 'partial_success'
    assert restored['task_committed'] is True and restored['retry_safe'] is False
    assert restored['task']['instance_id'] == saved[0].instance_id


@pytest.mark.asyncio
@pytest.mark.parametrize('entrypoint', ['rest', 'mcp'])
async def test_failed_terminal_write_never_publishes_completed(mcp_context, api_context, monkeypatch, entrypoint):
    generation = api_context['task_generation_service']
    original_change = GenerationReceipts.change

    async def change(self, job_id, **changes):
        if changes.get('status') == 'completed':
            raise OSError('fixture terminal write failure')
        return await original_change(self, job_id, **changes)

    monkeypatch.setattr(GenerationReceipts, 'change', change)
    job, coroutine = await job_worker(mcp_context, api_context, monkeypatch, entrypoint)
    await coroutine
    live = await generation.get_job(job.job_id)
    assert live.status == 'partial_success'
    result = (await task_routes.get_task_generation_job(job.job_id, TaskGenerationService(), service(mcp_context)))['job']
    assert result['status'] == 'partial_success'
    assert result['task_committed'] is True and result['retry_safe'] is False
    assert result['task']['instance_id'] == live.task.instance_id


@pytest.mark.asyncio
@pytest.mark.parametrize('entrypoint', ['rest', 'mcp'])
async def test_completed_observation_survives_worker_cancellation_and_restart(mcp_context, api_context, monkeypatch, entrypoint):
    generation = api_context['task_generation_service']
    reached, hold = asyncio.Event(), asyncio.Event()
    publish = generation.publish

    async def paused_publish(job_id, data):
        result = await publish(job_id, data)
        if data['status'] == 'completed':
            reached.set()
            await hold.wait()
        return result

    monkeypatch.setattr(generation, 'publish', paused_publish)
    job, coroutine = await job_worker(mcp_context, api_context, monkeypatch, entrypoint)
    worker = asyncio.create_task(coroutine)
    await asyncio.wait_for(reached.wait(), 2)
    before = await GenerationReceipts(service(mcp_context).repository).observe(job.job_id, generation)
    # Recovery may publish the already durable terminal fact a second time.
    monkeypatch.setattr(generation, 'publish', publish)
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker
    after = await GenerationReceipts(service(mcp_context).repository).observe(job.job_id, TaskGenerationService())
    assert before['status'] == 'completed'
    assert after['status'] == 'completed'
    assert after['task'] == before['task']
    assert after['task_committed'] is True and after['retry_safe'] is False
