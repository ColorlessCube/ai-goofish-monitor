"""Independent review probes. Fixture SQLite only; network/crawler prohibited."""
import asyncio
import contextlib
import importlib
import io
import json
import socket
import sys
import threading
import types
from pathlib import Path

import httpx
import pytest

from test_mcp_task_tools import mcp_context, invoke
from src.api import dependencies as deps
from src.services import mcp_task_operations as ops
from src.services import task_generation_runner as runner
from src.services.generation_receipts import GenerationReceipts
from src.services.task_generation_service import TaskGenerationService
from src.domain.models.task import TaskCreate, TaskGenerateRequest, TaskUpdate
from src.api.routes import tasks as task_routes


@pytest.fixture(autouse=True)
def prohibit_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError('NETWORK FORBIDDEN IN REVIEW')
    monkeypatch.setattr(socket.socket, 'connect', blocked)
    monkeypatch.setattr(socket, 'create_connection', blocked)


def service(ctx):
    return ctx['app'].dependency_overrides[deps.get_task_service]()


@pytest.mark.asyncio
async def test_rest_keyword_generate_postcommit_failure_exposes_task(mcp_context, api_context, monkeypatch):
    async def failed(*args):
        raise RuntimeError('fixture scheduler offline')
    monkeypatch.setattr(api_context['scheduler_service'], 'reload_jobs', failed)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=mcp_context['app']), base_url='http://fixture') as client:
        response = await client.post('/api/tasks/generate', json={
            'task_name': 'keyword generation', 'keyword': 'camera',
            'decision_mode': 'keyword', 'keyword_rules': ['working'], 'cron': '0 * * * *'})
    saved = await service(mcp_context).get_all_tasks()
    print(json.dumps({'case': 'keyword_commit', 'http_status': response.status_code,
        'response': response.json(), 'persisted': [t.model_dump() for t in saved]}))
    assert len(saved) == 1
    assert response.json().get('task', {}).get('instance_id') == saved[0].instance_id
    assert response.json().get('status') == 'partial_success'
    assert response.json()['task_committed'] is True
    assert response.json()['retry_safe'] is False
    assert response.json()['error'] == 'scheduler_sync_required'
    assert response.json()['task']['enabled'] is True


@pytest.mark.asyncio
async def test_rest_never_publishes_completed_before_terminal_write(mcp_context, api_context, monkeypatch):
    generation = api_context['task_generation_service']
    svc = service(mcp_context)
    job = await generation.create_job('completion durability')
    reached, hold = asyncio.Event(), asyncio.Event()
    original_change = GenerationReceipts.change
    async def paused_terminal(self, job_id, **changes):
        if changes.get('status') == 'completed':
            reached.set()
            await hold.wait()
        return await original_change(self, job_id, **changes)
    async def generated(**kwargs):
        return 'fixture criteria'
    monkeypatch.setattr(GenerationReceipts, 'change', paused_terminal)
    monkeypatch.setattr(runner, 'generate_criteria', generated)
    worker = asyncio.create_task(runner.run_ai_generation_job(job_id=job.job_id,
        req=TaskGenerateRequest(task_name=job.task_name, keyword='camera', description='working'),
        task_service=svc, scheduler_service=api_context['scheduler_service'], generation_service=generation))
    await asyncio.wait_for(reached.wait(), 2)
    before = (await task_routes.get_task_generation_job(job.job_id, generation, svc))['job']
    hold.set()
    await worker
    completed = (await task_routes.get_task_generation_job(job.job_id, generation, svc))['job']
    after = (await task_routes.get_task_generation_job(job.job_id, TaskGenerationService(), svc))['job']
    print(json.dumps({'case': 'completed_drift', 'before': before['status'], 'after': after['status'],
        'task_committed': after.get('task_committed')}))
    # A paused terminal write must not publish completed. Once observed, the
    # terminal receipt and immutable task must survive a fresh service instance.
    assert before['status'] != 'completed'
    assert completed['status'] == 'completed'
    assert after['status'] == 'completed', 'A completed receipt already observed over REST becomes unknown after restart'
    assert completed['task'] == after['task']


@pytest.mark.asyncio
@pytest.mark.parametrize('entrypoint', ['rest', 'mcp'])
async def test_cancelled_worker_is_not_reported_running_forever(mcp_context, api_context, monkeypatch, entrypoint):
    generation = api_context['task_generation_service']
    svc = service(mcp_context)
    exceptions = []
    monkeypatch.setattr(threading, 'excepthook', lambda args: exceptions.append(type(args.exc_value).__name__))
    async def cancelled(*args):
        raise asyncio.CancelledError('fixture cancellation after task commit')
    async def generated(**kwargs):
        return 'fixture criteria'
    monkeypatch.setattr(api_context['scheduler_service'], 'reload_jobs', cancelled)
    monkeypatch.setattr(runner, 'generate_criteria', generated)
    job = await generation.create_job('worker cancelled')
    await GenerationReceipts(svc.repository).create(job)
    if entrypoint == 'rest':
        coroutine = runner.run_ai_generation_job(job_id=job.job_id,
            req=TaskGenerateRequest(task_name=job.task_name, keyword='camera', description='working'),
            task_service=svc, scheduler_service=api_context['scheduler_service'], generation_service=generation)
    else:
        from src.mcp.task_tools import GenerateTask
        model = GenerateTask(task_name=job.task_name, keyword='camera', decision_mode='keyword', keyword_rules=['ok'])
        coroutine = ops.run_generation(model, mcp_context, job.job_id, generation)
    generation.track(coroutine, job_id=job.job_id)
    # Wait for the real worker thread to finish (without scheduling any crawler).
    workers = list(generation._workers)
    for thread in workers:
        await asyncio.to_thread(thread.join, 3)
    assert not generation._workers
    assert exceptions == ['CancelledError']
    actual = await svc.get_all_tasks()
    if entrypoint == 'rest':
        result = (await task_routes.get_task_generation_job(job.job_id, generation, svc))['job']
    else:
        result = (await invoke('get_generation_job', {'job_id': job.job_id}, mcp_context))['job']
    print(json.dumps({'case': 'cancelled_worker', 'entrypoint': entrypoint,
        'worker_count': len(generation._workers), 'task_count': len(actual),
        'reported_status': result['status'], 'reported_task': bool(result.get('task'))}))
    assert actual
    assert result['status'] in {'unknown', 'partial_success'}, 'A dead worker is not running'
    assert result.get('task'), 'Committed task must not be hidden by stale memory job'


@pytest.mark.asyncio
@pytest.mark.parametrize('entrypoint', ['rest', 'mcp'])
async def test_commit_succeeded_but_unlock_failed_is_not_generation_failed(mcp_context, api_context, monkeypatch, entrypoint):
    svc = service(mcp_context)
    generation = api_context['task_generation_service']
    job = await generation.create_job('postcommit unlock error')
    receipts = GenerationReceipts(svc.repository)
    await receipts.create(job)
    from src.services import prompt_assets as assets
    import sqlite3
    original_flock = assets.fcntl.flock
    injected = []
    def fail_first_unlock_after_commit(fd, operation):
        result = original_flock(fd, operation)
        if operation == assets.fcntl.LOCK_UN and not injected:
            with sqlite3.connect(svc.repository.db_path) as conn:
                count = conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]
            if count:
                injected.append(True)
                raise OSError('fixture unlock I/O error after successful COMMIT')
        return result
    async def generated(**kwargs):
        return 'fixture criteria'
    monkeypatch.setattr(assets.fcntl, 'flock', fail_first_unlock_after_commit)
    monkeypatch.setattr(runner, 'generate_criteria', generated)
    if entrypoint == 'rest':
        await runner.run_ai_generation_job(job_id=job.job_id,
            req=TaskGenerateRequest(task_name=job.task_name, keyword='camera', description='working'),
            task_service=svc, scheduler_service=api_context['scheduler_service'], generation_service=generation)
    else:
        from src.mcp.task_tools import GenerateTask
        model = GenerateTask(task_name=job.task_name, keyword='camera', decision_mode='keyword', keyword_rules=['ok'])
        await ops.run_generation(model, mcp_context, job.job_id, generation)
    actual = await svc.get_all_tasks()
    stored = await receipts.get(job.job_id)
    live = await generation.get_job(job.job_id)
    print(json.dumps({'case': 'commit_unlock_error', 'entrypoint': entrypoint,
        'task_count': len(actual), 'durable_status': stored['status'],
        'task_committed': stored.get('task_committed'), 'live_task': bool(live.task), 'live_status': live.status}))
    assert injected and actual
    assert stored['status'] in {'partial_success', 'unknown'}
    assert live.task is not None


@pytest.mark.asyncio
async def test_process_checked_version_is_checked_by_runtime(mcp_context, api_context, monkeypatch):
    from src.services.process_service import ProcessService
    svc = service(mcp_context)
    monkeypatch.setenv('APP_DATABASE_FILE', str(api_context['db_path']))
    task = await svc.create_task(TaskCreate(task_name='checked name', keyword='AUTHORIZED',
        decision_mode='keyword', keyword_rules=['ok'], account_strategy='ass'))
    captured = []
    fake_scraper = types.ModuleType('src.scraper')
    async def scrape(task_config, debug_limit):
        captured.append(task_config)
        return 0
    fake_scraper.scrape_xianyu = scrape
    monkeypatch.setitem(sys.modules, 'src.scraper', fake_scraper)
    sys.modules.pop('spider_v2', None)
    spider = importlib.import_module('spider_v2')
    process = ProcessService()
    monkeypatch.setattr(process.failure_guard, 'should_skip_start', lambda *a, **k: types.SimpleNamespace(skip=False))
    monkeypatch.setattr(process, '_open_log_file', lambda *a: ('fixture.log', io.StringIO()))
    monkeypatch.setattr(process, '_register_runtime', lambda *a: None)
    async def simulated_spawn(name, log_handle, *, task_id, instance_id, config_version=None):
        binding = {'config_version': config_version} if config_version is not None else {}
        command = process._build_spawn_command(name, task_id=task_id, instance_id=instance_id, **binding)
        # Real SQLite edit after ProcessService checked version, before child loads DB.
        await svc.update_task(task_id, TaskUpdate(keyword='NOT AUTHORIZED', task_name='edited name'))
        monkeypatch.setattr(sys, 'argv', command[2:])
        await spider.main()
        log_handle.close()
        return types.SimpleNamespace(pid=12345)
    monkeypatch.setattr(process, '_spawn_process', simulated_spawn)
    accepted = await process.start_task(task.id, task.task_name,
        instance_id=task.instance_id, config_version=task.config_version)
    print(json.dumps({'case': 'runtime_version_race', 'accepted': accepted,
        'authorized_version': task.config_version, 'runtime': [{'keyword': c['keyword'],
        'config_version': c['config_version'], 'task_name': c['task_name']} for c in captured]}))
    assert accepted, 'The simulated spawn must actually exercise child loading, not fail on a stale mock signature'
    assert not captured or captured[0]['keyword'] == 'AUTHORIZED'
