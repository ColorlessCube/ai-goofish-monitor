"""Portable MCP security regressions: fixture tools and temporary journals only."""
import asyncio
import json
from pathlib import Path
import select
import sqlite3
import subprocess
import sys
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from src.mcp import journal, remote
from tests.unit import test_mcp_remote as fixtures


ROOT = Path(__file__).resolve().parents[2]


def read_write_and_receipt(monkeypatch, tmp_path, payload):
    client = fixtures.client_for(monkeypatch, tmp_path)
    tools = fixtures.install_tools(monkeypatch)

    async def sensitive(args, ctx):
        return payload

    tools[0]['handler'] = tools[1]['handler'] = sensitive
    read = fixtures.rpc(client, 'tools/call', {'name': 'read_fixture'})
    request_id = str(uuid4())
    params = {'name': 'write_fixture', 'arguments': {'request_id': request_id}}
    write = fixtures.rpc(client, 'tools/call', params, token=fixtures.WRITE_TOKEN)
    for response in (read, write):
        assert response.status_code == 200
        assert response.json()['result']['isError'] is False
    with sqlite3.connect(tmp_path / 'mcp-state' / 'invocations.sqlite3') as db:
        row = db.execute('SELECT response_json FROM invocations WHERE request_id=?', (request_id,)).fetchone()
    assert row is not None and row[0] is not None
    assert json.loads(row[0]) == write.json()['result']
    replay = fixtures.rpc(client, 'tools/call', params, token=fixtures.WRITE_TOKEN)
    assert replay.status_code == 200 and replay.json()['result'] == write.json()['result']
    return {'read': read.text, 'write': write.text, 'receipt': row[0], 'replay': replay.text}


@pytest.mark.parametrize('payload,marker', [
    ({'log': '{"api_key": "fixture-json-api-secret-903"}'}, 'fixture-json-api-secret-903'),
    ({'log': 'upstream rejected Bearer fixture-bearer-secret-903'}, 'fixture-bearer-secret-903'),
    ({'token': 'fixture-generic-token-903'}, 'fixture-generic-token-903'),
])
def test_non_environment_secrets_not_returned_or_persisted(monkeypatch, tmp_path, payload, marker):
    outputs = read_write_and_receipt(monkeypatch, tmp_path, payload)
    exposed = {name: marker in raw for name, raw in outputs.items()}
    assert not any(exposed.values()), exposed


def test_environment_secret_used_as_dictionary_key_is_scrubbed(monkeypatch, tmp_path):
    marker = 'fixture-runtime-secret-903'
    monkeypatch.setenv('FIXTURE_API_KEY', marker)
    outputs = read_write_and_receipt(monkeypatch, tmp_path, {marker: 'ordinary-value'})
    exposed = {name: marker in raw for name, raw in outputs.items()}
    assert not any(exposed.values()), exposed


def test_token_counts_groups_and_prompt_configuration_are_preserved(monkeypatch, tmp_path):
    payload = {
        'usage': {'prompt_tokens': 23, 'completion_tokens': 8, 'total_tokens': 31},
        'tokens': {'prompt': 23, 'completion': 8},
        'token': {'prompt': 23, 'completion': 8},
        'prompt_config': {'max_tokens': 1024, 'token_limit': 2048, 'tokenizer': 'fixture-encoding', 'token': 512},
        'log': 'prompt_tokens=23 max_tokens=1024 token_limit=2048 tokenizer=fixture-encoding token=512',
    }
    outputs = read_write_and_receipt(monkeypatch, tmp_path, payload)
    for name, raw in outputs.items():
        result = json.loads(raw) if name == 'receipt' else json.loads(raw)['result']
        content = dict(result['structuredContent'])
        content.pop('_receipt', None)
        assert content == payload
        assert json.loads(result['content'][0]['text']) == result['structuredContent']


def test_concurrent_same_id_never_reexecutes(monkeypatch, tmp_path):
    fixtures.client_for(monkeypatch, tmp_path)
    tools = fixtures.install_tools(monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    effects = []

    async def mutate(args, ctx):
        effects.append(1)
        entered.set()
        await release.wait()
        return {'ok': True}

    tools[1]['handler'] = mutate
    ctx = {'principal': {'subject': 'writer:ass-mcp'}}
    request_id = str(uuid4())

    async def run():
        first = asyncio.create_task(remote.execute_mutation(tools[1], fixtures.EchoArgs(), {}, request_id, ctx))
        await asyncio.wait_for(entered.wait(), timeout=5)
        second = await remote.execute_mutation(tools[1], fixtures.EchoArgs(), {}, request_id, ctx)
        assert second['isError'] and second['structuredContent']['error'] == 'write_busy'
        release.set()
        original = await first
        cached = await remote.execute_mutation(tools[1], fixtures.EchoArgs(), {}, request_id, ctx)
        assert cached == original

    asyncio.run(run())
    assert effects == [1]


def test_cancelled_handler_never_reexecutes(monkeypatch, tmp_path):
    fixtures.client_for(monkeypatch, tmp_path)
    tools = fixtures.install_tools(monkeypatch)
    entered = asyncio.Event()
    effects = []

    async def mutate(args, ctx):
        effects.append(1)
        entered.set()
        await asyncio.Event().wait()

    tools[1]['handler'] = mutate
    ctx = {'principal': {'subject': 'writer:ass-mcp'}}
    request_id = str(uuid4())

    async def run():
        first = asyncio.create_task(remote.execute_mutation(tools[1], fixtures.EchoArgs(), {}, request_id, ctx))
        await asyncio.wait_for(entered.wait(), timeout=5)
        first.cancel()
        original = await first
        assert original['isError'] and original['structuredContent']['_receipt']['status'] == 'unknown'
        cached = await remote.execute_mutation(tools[1], fixtures.EchoArgs(), {}, request_id, ctx)
        assert cached == original

    asyncio.run(run())
    assert effects == [1]


def test_duplicate_security_headers_denied(monkeypatch, tmp_path):
    client = fixtures.client_for(monkeypatch, tmp_path)
    monkeypatch.setenv('GOOFISH_MCP_ALLOWED_ORIGINS', 'https://approved.example')
    base = [('Authorization', 'Bearer ' + fixtures.READ_TOKEN), ('Accept', 'application/json, text/event-stream'), ('Content-Type', 'application/json')]
    for name, values in [('Host', ['testserver', 'evil.example']), ('Origin', ['https://approved.example', 'https://evil.example']), ('Authorization', ['Bearer ' + fixtures.WRITE_TOKEN])]:
        response = client.post('/api/mcp', content='{"jsonrpc":"2.0","id":1,"method":"ping"}', headers=base + [(name, value) for value in values])
        assert response.status_code in (401, 403), (name, response.status_code)


def test_invalid_utf8_surrogate_id_does_not_escape_rpc_error(monkeypatch, tmp_path):
    client = fixtures.client_for(monkeypatch, tmp_path)
    client = TestClient(client.app, raise_server_exceptions=False)
    response = client.post('/api/mcp', content=b'{"jsonrpc":"2.0","id":"\\ud800","method":"ping"}', headers={'Authorization': 'Bearer ' + fixtures.READ_TOKEN, 'Accept': 'application/json, text/event-stream', 'Content-Type': 'application/json'})
    assert response.status_code == 400, (response.status_code, response.text)
    assert response.json()['error']['code'] == -32600
    assert response.json()['id'] is None
    assert '\\ud800' not in response.text.lower()


@pytest.mark.parametrize('body', [
    {'jsonrpc': '2.0', 'id': '\udfff', 'method': 'ping'},
    {'jsonrpc': '2.0', 'id': 1, 'method': '\ud800'},
    {'jsonrpc': '2.0', 'id': 1, 'method': 'ping', 'params': {'nested': ['\ud800']}},
    {'jsonrpc': '2.0', 'id': 1, 'method': 'ping', 'params': {'\ud800': 'value'}},
    {'jsonrpc': '2.0', 'method': 'notifications/initialized', 'params': {'value': '\ud800'}},
])
def test_invalid_unicode_is_rejected_before_dispatch(monkeypatch, tmp_path, body):
    client = fixtures.client_for(monkeypatch, tmp_path)
    client = TestClient(client.app, raise_server_exceptions=False)
    dispatched = []

    async def track_dispatch(body, request, principal):
        dispatched.append(body)
        return remote.result(body['id'], {})

    monkeypatch.setattr(remote, 'dispatch', track_dispatch)
    response = client.post('/api/mcp', content=json.dumps(body).encode('utf-8'), headers={'Authorization': 'Bearer ' + fixtures.READ_TOKEN, 'Accept': 'application/json, text/event-stream', 'Content-Type': 'application/json'})
    assert response.status_code == 400
    assert response.json() == remote.error(None, -32600, 'invalid_request')
    assert dispatched == []


@pytest.mark.parametrize('ensure_ascii', [True, False])
def test_valid_unicode_rpc_ids_remain_supported(monkeypatch, tmp_path, ensure_ascii):
    client = fixtures.client_for(monkeypatch, tmp_path)
    request_id = '请求-\U0001f680'
    body = {'jsonrpc': '2.0', 'id': request_id, 'method': 'ping'}
    response = client.post('/api/mcp', content=json.dumps(body, ensure_ascii=ensure_ascii).encode('utf-8'), headers={'Authorization': 'Bearer ' + fixtures.READ_TOKEN, 'Accept': 'application/json, text/event-stream', 'Content-Type': 'application/json'})
    assert response.status_code == 200
    assert response.json() == remote.result(request_id, {})


def test_response_encoding_error_stays_inside_rpc_error_boundary(monkeypatch, tmp_path):
    client = fixtures.client_for(monkeypatch, tmp_path)

    async def invalid_response(body, request, principal):
        return remote.result(body['id'], {'value': '\ud800'})

    monkeypatch.setattr(remote, 'dispatch', invalid_response)
    response = fixtures.rpc(client, 'ping')
    assert response.status_code == 200
    assert response.json() == remote.error(1, -32603, 'server_error')
    assert response.headers['cache-control'] == 'no-store'


def test_timeout_is_unknown_and_never_reexecutes(monkeypatch, tmp_path):
    fixtures.client_for(monkeypatch, tmp_path)
    tools = fixtures.install_tools(monkeypatch)
    effects = []
    original_timeout = asyncio.timeout
    monkeypatch.setattr(remote.asyncio, 'timeout', lambda seconds: original_timeout(0.01))

    async def mutate(args, ctx):
        effects.append(1)
        await asyncio.Event().wait()

    tools[1]['handler'] = mutate
    ctx = {'principal': {'subject': 'writer:ass-mcp'}}
    request_id = str(uuid4())

    async def run():
        first = await remote.execute_mutation(tools[1], fixtures.EchoArgs(), {}, request_id, ctx)
        assert first['isError'] and first['structuredContent']['_receipt']['status'] == 'unknown'
        assert await remote.execute_mutation(tools[1], fixtures.EchoArgs(), {}, request_id, ctx) == first

    asyncio.run(run())
    assert effects == [1]


def test_claim_survives_killed_process_without_replay(monkeypatch, tmp_path):
    fixtures.client_for(monkeypatch, tmp_path)
    tools = fixtures.install_tools(monkeypatch)
    request_id = str(uuid4())
    code = '''
import sys, time
from src.mcp import journal
journal.claim('writer:ass-mcp', sys.argv[1], 'write_fixture', {})
print('claimed', flush=True)
time.sleep(60)
'''
    child = subprocess.Popen([sys.executable, '-c', code, request_id], cwd=ROOT, stdout=subprocess.PIPE, text=True)
    assert child.stdout is not None
    try:
        assert select.select([child.stdout], [], [], 5)[0], 'claim process did not become ready'
        assert child.stdout.readline().strip() == 'claimed'
        child.kill()
        child.wait(timeout=5)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        child.stdout.close()
    effects = []

    async def mutate(args, ctx):
        effects.append(1)
        return {'ok': True}

    tools[1]['handler'] = mutate
    response = asyncio.run(remote.execute_mutation(tools[1], fixtures.EchoArgs(), {}, request_id, {'principal': {'subject': 'writer:ass-mcp'}}))
    assert response['isError'] and response['structuredContent']['_receipt']['status'] == 'running_or_unknown'
    assert effects == []


def test_failed_finalization_cannot_report_success_or_replay(monkeypatch, tmp_path):
    client = fixtures.client_for(monkeypatch, tmp_path)
    tools = fixtures.install_tools(monkeypatch)
    effects = []

    async def mutate(args, ctx):
        effects.append(1)
        return {'ok': True}

    tools[1]['handler'] = mutate

    def broken_finish(*args):
        raise RuntimeError('fixture-sensitive-db-exception-903')

    monkeypatch.setattr(journal, 'finish', broken_finish)
    params = {'name': 'write_fixture', 'arguments': {'request_id': str(uuid4())}}
    first = fixtures.rpc(client, 'tools/call', params, token=fixtures.WRITE_TOKEN).json()
    assert first['error']['code'] == -32603 and 'fixture-sensitive-db-exception-903' not in json.dumps(first)
    second = fixtures.rpc(client, 'tools/call', params, token=fixtures.WRITE_TOKEN).json()['result']
    assert second['isError'] and second['structuredContent']['_receipt']['status'] == 'running_or_unknown'
    assert effects == [1]
