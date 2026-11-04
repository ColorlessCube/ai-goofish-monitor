"""Positive compatibility checks for the independent final review."""
import asyncio
import importlib
import json
import sys
import types
from pathlib import Path
import httpx
import pytest

from test_runtime_receipt_review import (
    mcp_context, invoke, service, prohibit_network,
    deps, runner, task_routes, TaskCreate, TaskUpdate, TaskGenerateRequest,
    TaskGenerationService,
)


@pytest.mark.asyncio
@pytest.mark.parametrize('entrypoint', ['rest', 'mcp'])
@pytest.mark.parametrize('change', [{'enabled': False}, {'cron': None}])
async def test_disable_unschedule_with_both_ai_assets_missing(mcp_context, api_context, entrypoint, change):
    original = await invoke('create_task', {'task_name': 'risk reduction', 'keyword': 'camera',
        'description': 'working', 'enabled': True, 'cron': '0 * * * *',
        'ai_prompt_criteria_file': 'prompts/criteria.txt'}, mcp_context)
    Path('prompts/base_prompt.txt').unlink()
    Path('prompts/criteria.txt').unlink()
    current = await invoke('get_task', {'task_id': original['task']['id']}, mcp_context)
    if entrypoint == 'mcp':
        result = await invoke('update_task', {'task_id': original['task']['id'],
            'expected_revision': current['revision'], 'changes': change}, mcp_context)
    else:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=mcp_context['app']), base_url='http://fixture') as client:
            response = await client.patch(f"/api/tasks/{original['task']['id']}", json=change)
        assert response.status_code == 200, response.text
        result = response.json()
    for key, value in change.items():
        assert result['task'][key] == value
    assert api_context['process_service'].started == []


@pytest.mark.asyncio
async def test_cli_duplicate_names_exact_id_instance_and_controlled_bare_prompt(mcp_context, monkeypatch):
    records = [dict(id=index, instance_id=f'instance{index}', task_name='duplicate', enabled=True,
        keyword=f'keyword{index}', account_strategy='ass', decision_mode='ai',
        ai_prompt_base_file='base_prompt.txt', ai_prompt_criteria_file='criteria.txt') for index in range(2)]
    Path('base_prompt.txt').write_text('UNREVIEWED CWD {{CRITERIA_SECTION}}')
    Path('criteria.txt').write_text('UNREVIEWED CWD CRITERIA')
    Path('config.json').write_text(json.dumps(records))
    captured = []
    fake = types.ModuleType('src.scraper')
    async def scrape(task_config, debug_limit):
        captured.append(task_config)
        return 0
    fake.scrape_xianyu = scrape
    monkeypatch.setitem(sys.modules, 'src.scraper', fake)
    sys.modules.pop('spider_v2', None)
    spider = importlib.import_module('spider_v2')
    monkeypatch.setattr(sys, 'argv', ['spider_v2.py', '--config', 'config.json', '--task-id', '1', '--task-instance', 'instance1'])
    await spider.main()
    assert len(captured) == 1 and captured[0]['keyword'] == 'keyword1'
    assert captured[0]['ai_prompt_text'] == 'Base Only normal functioning devices'
    captured.clear()
    monkeypatch.setattr(sys, 'argv', ['spider_v2.py', '--config', 'config.json', '--task-name', 'duplicate'])
    await spider.main()
    assert not captured


@pytest.mark.asyncio
async def test_rest_known_completed_snapshot_does_not_follow_live_edits_or_replacement(mcp_context, api_context, monkeypatch):
    generation = api_context['task_generation_service']
    svc = service(mcp_context)
    job = await generation.create_job('immutable original')
    async def generated(**kwargs):
        return 'fixture criteria'
    monkeypatch.setattr(runner, 'generate_criteria', generated)
    await runner.run_ai_generation_job(job_id=job.job_id,
        req=TaskGenerateRequest(task_name=job.task_name, keyword='camera', description='working'),
        task_service=svc, scheduler_service=api_context['scheduler_service'], generation_service=generation)
    original = (await task_routes.get_task_generation_job(job.job_id, generation, svc))['job']
    original_task = original['task']
    await svc.update_task(original_task['id'], TaskUpdate(task_name='later edit', keyword='changed keyword'))
    live = (await task_routes.get_task_generation_job(job.job_id, generation, svc))['job']
    assert live['task'] == original_task
    await svc.delete_task(original_task['id'])
    replacement = await svc.create_task(TaskCreate(task_name='unrelated replacement', keyword='unrelated',
        decision_mode='keyword', keyword_rules=['ok']))
    restored = (await task_routes.get_task_generation_job(job.job_id, TaskGenerationService(), svc))['job']
    assert restored['status'] == 'completed'
    assert restored['task'] == original_task
    assert restored['task']['instance_id'] != replacement.instance_id
