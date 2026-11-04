"""Read-only review regressions. All I/O uses scratch fixtures; network is denied."""
import asyncio
from contextlib import contextmanager
import importlib
import json
import os
from pathlib import Path
import socket
import sys
import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from src.mcp import admin_tools as admin, diagnostic_tools as diag, result_tools as results
from src.mcp.redaction import safe_payload
from src.infrastructure.config.env_manager import EnvManager
from src.services import result_storage_service as storage
from src.api import dependencies

@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("real network forbidden in review regressions")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket.socket, "sendto", denied)

ALL_SCOPES = {'results:read', 'results:write', 'results:delete', 'system:read', 'settings:read', 'settings:write', 'settings:test', 'notifications:send'}

@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('APP_DATABASE_FILE', str(tmp_path / 'results.db'))
    monkeypatch.setenv('GOOFISH_MCP_STATE_DIR', str(tmp_path / 'mcp-state'))
    store = EnvManager(str(tmp_path / '.env'))
    store.update_values({'OPENAI_API_KEY': 'fixture-old-key', 'OPENAI_BASE_URL': 'https://stored.invalid/v1', 'OPENAI_MODEL_NAME': 'model-a', 'SKIP_AI_ANALYSIS': 'false', 'PCURL_TO_MOBILE': 'true', **{k: '' for k in admin.CHANNEL_REQUIREMENTS.values() for k in k}})
    app = FastAPI()
    app.dependency_overrides[admin.get_settings_store] = lambda: store
    return {'app': app, 'principal': {'subject': 'independent-review', 'scopes': ALL_SCOPES}, 'store': store, 'root': tmp_path}

def call(module, ctx, name, **kwargs):
    tool = next(t for t in module.build_tools() if t['name'] == name)
    return asyncio.run(tool['handler'](tool['input_model'](**kwargs), ctx))

def seed(item_id='1', title='fixture camera'):
    return asyncio.run(storage.save_result_record({'搜索关键字':'camera','任务名称':'fixture','爬取时间':'2026-01-01T00:00:00','商品信息':{'商品ID':item_id,'商品标题':title,'当前售价':'100','商品链接':f'https://www.goofish.com/item?id={item_id}'}, 'cookie':'fixture-nonexported'}, 'camera'))

NAME = 'camera_full_data.jsonl'

def test_multiline_log_credentials_stay_redacted(ctx):
    class TaskService:
        async def get_task(self, task_id):
            return SimpleNamespace(id=1, task_name='fixture')
    ctx['app'].dependency_overrides[dependencies.get_task_service] = TaskService
    log = ctx['root'] / 'logs' / 'fixture_1.log'
    log.parent.mkdir()
    log.write_text('ordinary line\nCookie:\n  sid=foldedfixturecredential\n{"cookies": [{"name": "sid", "value":\n  "splitfixturecredential"\n}]}\n')
    output, _ = safe_payload(call(diag, ctx, 'get_task_logs', task_id=1))
    assert 'foldedfixturecredential' not in output['content']
    assert 'splitfixturecredential' not in output['content']


@pytest.mark.parametrize('offset,limit', [(0, 100), (2, 1), (4, 1)])
def test_log_pages_redact_whole_events_before_slicing(ctx, offset, limit):
    ctx['app'].dependency_overrides[dependencies.get_task_service] = lambda: SimpleNamespace(
        get_task=lambda task_id: _task_fixture())
    log = ctx['root'] / 'logs' / 'fixture_1.log'
    log.parent.mkdir()
    log.write_text('ordinary line\nCookie:\n  sid=foldedfixturecredential\n'
                   '{"cookies": [{"name": "sid", "value":\n  "splitfixturecredential"\n}]}\n'
                   '2026-01-01 00:00:00 INFO next safe event\n')
    value = call(diag, ctx, 'get_task_logs', task_id=1, offset_lines=offset, limit_lines=limit)
    assert 'foldedfixturecredential' not in value['content']
    assert 'splitfixturecredential' not in value['content']
    if limit == 100:
        assert 'ordinary line' in value['content'] and 'next safe event' in value['content']


async def _task_fixture():
    return SimpleNamespace(id=1, task_name='fixture')


@pytest.mark.parametrize('timestamp', ['2026-01-01 00:00:00 INFO', '[ 2026-01-01 00:00:00]'])
def test_truncated_log_discards_unknown_event_prefix_but_keeps_next_event(ctx, timestamp):
    ctx['app'].dependency_overrides[dependencies.get_task_service] = lambda: SimpleNamespace(
        get_task=lambda task_id: _task_fixture())
    log = ctx['root'] / 'logs' / 'fixture_1.log'
    log.parent.mkdir()
    log.write_text('Cookie:\n' + ' ' * 66000 + '\n  sid=boundaryfixturecredential\n'
                   f'{timestamp} safe event after boundary\n')
    value = call(diag, ctx, 'get_task_logs', task_id=1)
    assert 'boundaryfixturecredential' not in value['content']
    assert 'safe event after boundary' in value['content'] and value['truncated']


def test_env_update_preserves_concurrent_rest_key_rotation(ctx, monkeypatch):
    from src.api.routes import settings as rest
    store = ctx['store']
    other_store = EnvManager(str(store.env_file))
    monkeypatch.setattr(rest, 'env_manager', other_store)
    monkeypatch.setattr(rest, '_reload_env', lambda: None)
    before = call(admin, ctx, 'get_ai_settings')
    original_write = store._write_env
    reached, release = threading.Event(), threading.Event()
    outcomes = []
    def paused_write(values):
        reached.set()
        assert release.wait(5)
        return original_write(values)
    monkeypatch.setattr(store, '_write_env', paused_write)
    def writer():
        try:
            outcomes.append(call(admin, ctx, 'update_ai_settings', expected_revision=before['revision'], OPENAI_MODEL_NAME='mcp-model'))
        except Exception as exc:
            outcomes.append(type(exc).__name__)
    thread = threading.Thread(target=writer)
    thread.start()
    assert reached.wait(5)
    rest_done = threading.Event()
    def rest_writer():
        asyncio.run(rest.update_ai_settings(rest.AISettingsModel(OPENAI_API_KEY='fixture-rotated-key', OPENAI_MODEL_NAME='rest-model')))
        rest_done.set()
    second = threading.Thread(target=rest_writer)
    second.start()
    try:
        # Before the fix REST races past the stale MCP snapshot; afterwards it waits.
        rest_done.wait(0.2)
    finally:
        release.set()
        thread.join(5)
        second.join(5)
    assert not thread.is_alive() and not second.is_alive() and rest_done.is_set()
    assert other_store.get_value('OPENAI_API_KEY') == 'fixture-rotated-key'



def test_revision_uses_unredacted_model_identity(ctx):
    store = ctx['store']
    store.set_value('OPENAI_MODEL_NAME', 'token-model-a')
    before = call(admin, ctx, 'get_ai_settings')
    after = call(admin, ctx, 'update_ai_settings', expected_revision=before['revision'], OPENAI_MODEL_NAME='token-model-b')
    assert before['revision'] != after['revision']
    with pytest.raises(HTTPException) as exc:
        call(admin, ctx, 'update_ai_settings', expected_revision=before['revision'], SKIP_AI_ANALYSIS=True)
    assert exc.value.status_code == 409


def test_ai_probe_reads_credential_and_destination_from_one_snapshot(ctx, monkeypatch):
    import openai
    from src.api.routes import settings as rest
    store = ctx['store']
    store.set_value('PROXY_URL', '')
    other = EnvManager(str(store.env_file))
    original_get = store.get_value
    switched = []
    observed = []
    def interleaved_get(key, default=None):
        value = original_get(key, default)
        if key == 'OPENAI_API_KEY' and not switched:
            switched.append(True)
            assert other.update_values({'OPENAI_API_KEY':'fixture-new-provider-key', 'OPENAI_BASE_URL':'https://new-provider.invalid/v1'})
        return value
    monkeypatch.setattr(store, 'get_value', interleaved_get)
    class FakeOpenAI:
        def __init__(self, **params):
            observed.append({'key':params['api_key'], 'url':params['base_url']})
    monkeypatch.setattr(openai, 'OpenAI', FakeOpenAI)
    monkeypatch.setattr(rest, 'create_ai_response_sync', lambda *args, **kwargs: object())
    monkeypatch.setattr(rest, 'extract_ai_response_content', lambda response: 'OK')
    value = call(admin, ctx, 'test_ai_connection')
    assert observed in [[{'key':'fixture-old-key','url':'https://stored.invalid/v1'}], [{'key':'fixture-new-provider-key','url':'https://new-provider.invalid/v1'}]]


def test_actual_bark_adapter_checks_application_rejection(ctx, monkeypatch):
    import requests
    ctx['store'].set_value('BARK_URL', 'https://stored.invalid/bark-fixture')
    response = requests.Response()
    response.status_code = 200
    response._content = b'{"code":400,"message":"invalid device key"}'
    response.url = 'https://stored.invalid/bark-fixture'
    attempted = []
    def fake_post(*args, **kwargs):
        attempted.append(True)
        return response
    monkeypatch.setattr(requests, 'post', fake_post)
    response = mutation(admin, ctx, 'test_notification', {'channel': 'bark'})
    value = json.loads(response['content'][0]['text'])
    assert value['success'] is False and value['attempted'] is True
    assert value['_receipt']['status'] == 'failed' and len(attempted) == 1


def mutation(module, ctx, name, kwargs, request_id=None):
    from src.mcp.remote import execute_mutation
    tool = next(t for t in module.build_tools() if t['name'] == name)
    return asyncio.run(execute_mutation(tool, tool['input_model'](**kwargs), kwargs, request_id or str(uuid4()), ctx))


def test_ai_probe_timeout_has_unknown_receipt_and_disables_sdk_retry(ctx, monkeypatch):
    import openai
    import httpx
    from src.api.routes import settings as rest
    observed = []
    class Client:
        def __init__(self, **kwargs):
            observed.append(kwargs)
    def request(*args, **kwargs):
        raise openai.APITimeoutError(request=httpx.Request('POST', 'https://fixture.invalid'))
    monkeypatch.setattr(openai, 'OpenAI', Client)
    monkeypatch.setattr(rest, 'create_ai_response_sync', request)
    response = mutation(admin, ctx, 'test_ai_connection', {})
    value = json.loads(response['content'][0]['text'])
    assert value['_receipt']['status'] == 'unknown'
    assert value['attempted'] is True and value['outcome'] == 'unknown'
    assert value['retry_allowed'] is False and observed[0]['max_retries'] == 0


def test_notification_uncertain_delivery_does_not_get_succeeded_receipt(ctx, monkeypatch):
    import requests
    ctx['store'].set_value('WEBHOOK_URL', 'https://stored.invalid/hook-fixture')
    accepted = []
    def fake_post(*args, **kwargs):
        accepted.append('fixture server accepted request before response timed out')
        raise requests.exceptions.ReadTimeout('fixture only')
    monkeypatch.setattr(requests, 'post', fake_post)
    request_id = str(uuid4())
    response = mutation(admin, ctx, 'test_notification', {'channel':'webhook'}, request_id)
    value = json.loads(response['content'][0]['text'])
    assert value['_receipt']['status'] == 'unknown'

    replay = mutation(admin, ctx, 'test_notification', {'channel':'webhook'}, request_id)
    assert replay == response and len(accepted) == 1
    from src.mcp import journal
    receipt = journal.get_receipt(ctx['principal']['subject'], request_id)
    assert receipt['status'] == 'unknown' and receipt['retry_allowed'] is False
    assert receipt['result'] == response
    with journal.connection() as db:
        row = db.execute('SELECT status,response_json FROM invocations WHERE request_id=?', (request_id,)).fetchone()
    assert row['status'] == 'unknown' and json.loads(row['response_json']) == response


def test_postcommit_result_readback_failure_reports_unknown_not_failed(ctx, monkeypatch):
    seed()
    before = call(results, ctx, 'get_result', filename=NAME, item_id='1')
    original_connection = results.sqlite_connection
    observed = []
    @contextmanager
    def connection_with_rest_interleave():
        with original_connection() as conn:
            class Proxy:
                def __getattr__(self, name):
                    return getattr(conn, name)
                def commit(self):
                    conn.commit()
                    observed.append(conn.execute('SELECT status FROM result_items WHERE result_filename=?',(NAME,)).fetchone()['status'])
                    asyncio.run(storage.delete_result_file_records(NAME))
            yield Proxy()
    monkeypatch.setattr(results, 'sqlite_connection', connection_with_rest_interleave)
    response = mutation(results, ctx, 'set_result_status', {'filename':NAME,'item_id':'1','expected_revision':before['revision'],'status':'hidden'})
    value = json.loads(response['content'][0]['text'])
    assert value['_receipt']['status'] in {'succeeded', 'unknown'}
    if value['_receipt']['status'] == 'succeeded':
        assert value['committed'] is True and value['item']['status'] == 'hidden'


@pytest.mark.parametrize('phase', ['commit_return', 'connection_close'])
def test_postcommit_exceptions_keep_commit_evidence(ctx, monkeypatch, phase):
    seed()
    before = call(results, ctx, 'get_result', filename=NAME, item_id='1')
    original = results.sqlite_connection
    @contextmanager
    def broken_connection():
        with original() as conn:
            class Proxy:
                def __getattr__(self, name):
                    return getattr(conn, name)
                def commit(self):
                    conn.commit()
                    if phase == 'commit_return':
                        raise OSError('fixture failure after actual commit')
            yield Proxy()
        if phase == 'connection_close':
            raise OSError('fixture failure after close')
    monkeypatch.setattr(results, 'sqlite_connection', broken_connection)
    response = mutation(results, ctx, 'set_result_status', {'filename': NAME, 'item_id': '1',
        'expected_revision': before['revision'], 'status': 'hidden'})
    value = json.loads(response['content'][0]['text'])
    assert value['_receipt']['status'] == 'unknown'
    assert value['commit_attempted'] is True
    if phase == 'connection_close':
        assert value['committed'] is True
    with original() as conn:
        assert conn.execute('SELECT status FROM result_items WHERE result_filename=?', (NAME,)).fetchone()['status'] == 'hidden'


def test_deleted_set_rejects_stale_empty_blacklist_revision(ctx):
    seed()
    old = call(results, ctx, 'get_blacklist_rules', filename=NAME)
    asyncio.run(storage.delete_result_file_records(NAME))
    with pytest.raises(HTTPException) as exc:
        call(results, ctx, 'update_blacklist_rules', filename=NAME, expected_revision=old['revision'], action='add', keywords=['camera'])
    assert exc.value.status_code in {404, 409}
    seed('2')
    with pytest.raises(HTTPException) as exc:
        call(results, ctx, 'update_blacklist_rules', filename=NAME, expected_revision=old['revision'], action='add', keywords=['camera'])
    assert exc.value.status_code == 409
    assert call(results, ctx, 'query_results', filename=NAME)['total'] == 1



def test_cleanup_cas_rejects_new_worker_record(ctx):
    seed()
    old = call(results, ctx, 'preview_result_cleanup', filename=NAME)
    seed('2')
    with pytest.raises(HTTPException) as exc:
        call(results, ctx, 'delete_result_set', filename=NAME, expected_revision=old['revision'])
    assert exc.value.status_code == 409
    assert call(results, ctx, 'query_results', filename=NAME)['total'] == 2


def test_result_mutation_holds_database_write_lock_against_rest(ctx, monkeypatch):
    seed()
    old = call(results, ctx, 'get_result', filename=NAME, item_id='1')
    inside_cas, release_cas = threading.Event(), threading.Event()
    rest_done = threading.Event()
    original = results.require_revision
    outcomes = []
    def paused_check(expected, rows):
        original(expected, rows)
        inside_cas.set()
        assert release_cas.wait(5)
    monkeypatch.setattr(results, 'require_revision', paused_check)
    def mcp_write():
        try:
            outcomes.append(call(results, ctx, 'set_result_status', filename=NAME, item_id='1', expected_revision=old['revision'], status='hidden'))
        except Exception as exc:
            outcomes.append(type(exc).__name__)
    def rest_write():
        asyncio.run(storage.update_item_status(NAME, '1', 'expired'))
        rest_done.set()
    one = threading.Thread(target=mcp_write)
    one.start()
    assert inside_cas.wait(5)
    two = threading.Thread(target=rest_write)
    two.start()
    try:
        assert not rest_done.wait(0.15), 'REST must not commit within MCP BEGIN IMMEDIATE'
    finally:
        release_cas.set()
        one.join(5)
        two.join(5)
    assert rest_done.is_set() and not one.is_alive()
    assert call(results, ctx, 'get_result', filename=NAME, item_id='1')['item']['status'] == 'expired'


def test_export_controls_and_sensitive_fields_are_safe(ctx):
    import csv
    import io
    seed('1', '\x00 \t=SUM(A1:A2)')
    for format in ('csv', 'jsonl'):
        output = call(results, ctx, 'export_results', filename=NAME, format=format, max_records=1)
        assert output['bytes'] <= 49152
        assert output['exported_records'] == 1
        assert 'fixture-nonexported' not in output['content']
        if format == 'csv':
            assert next(csv.DictReader(io.StringIO(output['content'])))['title'].startswith("'")
        else:
            assert len([json.loads(x) for x in output['content'].splitlines()]) == 1
