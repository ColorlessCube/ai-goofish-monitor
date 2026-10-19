"""ASS契约/权限/投影边界全部合成，httpx在传输边界隔离。"""
import copy
import json
from dataclasses import replace
from datetime import datetime, timezone
from uuid import UUID

import httpx
import pytest

from src.infrastructure.credentials import ass_session_client as module

NOW = 1800000000.0
TENANT = '10000000-0000-4000-8000-000000000001'
SECRET = '20000000-0000-4000-8000-000000000002'
KEY = '30000000-0000-4000-8000-000000000003'
TOKEN = 'ass_' + UUID(KEY).hex + '_' + 'x' * 43


def iso(t=NOW):
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec='milliseconds')


def bundle(version=1, mixed=False):
    value = {'format': 'ass-browser-session', 'schema_version': version, 'site_id': 'goofish', 'account_id': 'main',
             'producer_id': TENANT, 'snapshot_id': SECRET, 'ordering_mode': 'snapshot_created_at',
             'snapshot_created_at': iso(NOW - 100), 'observed_at': iso(), 'state': 'captured', 'reason': None,
             'source_origins': [module.PRIMARY_ORIGIN], 'cookies': [
                 {'name': 'sid', 'value': 'COOKIE_DUMMY', 'domain': '.goofish.com', 'path': '/', 'hostOnly': False,
                  'secure': True, 'httpOnly': True, 'sameSite': 'lax', 'session': True, 'storeId': '0'},
                 {'name': 'sid', 'value': 'COOKIE_HOST_DUMMY', 'domain': 'www.goofish.com', 'path': '/', 'hostOnly': True,
                  'secure': True, 'httpOnly': False, 'sameSite': 'strict', 'session': False, 'expirationDate': NOW + 100, 'storeId': '0'}],
             'pages': [{'origin': module.PRIMARY_ORIGIN, 'page_url': module.PRIMARY_ORIGIN + '/', 'referrer': None,
                        'visibility_state': 'visible', 'env': {'navigator': {'userAgent': 'Fixture desktop Macintosh', 'language': 'zh-CN', 'maxTouchPoints': 0},
                                                             'screen': {'width': 1280, 'height': 720, 'devicePixelRatio': 1},
                                                             'intl': {'timeZone': 'Asia/Shanghai'}},
                        'storage': {'local': {'fixture': 'X' * 118331}, 'session': {'fixture': 'STORAGE_DUMMY', '__proto__': 'not-code'}},
                        'headers': {'accept-language': 'zh-CN'}, 'headers_status': 'observed'}]}
    if version == 2:
        value.update(capture_mode='mixed' if mixed else 'complete', pages_observed_at=iso(NOW - 100000) if mixed else iso(),
                     page_baseline_id=KEY if mixed else SECRET)
    return value


@pytest.fixture
def settings(tmp_path):
    path = tmp_path / 'read-key'
    path.write_text(TOKEN + '\n')
    path.chmod(0o600)
    return module.AssSessionSettings('https://ass.invalid:18443', path, TENANT, SECRET, 'session.goofish.main')


def parsed(settings, version=1, mixed=False):
    return module.parse_session(json.dumps(bundle(version, mixed)), settings, now=NOW)


@pytest.mark.parametrize('version,mixed', [(1, False), (2, False), (2, True)])
def test_scopes_storage_and_component_age(settings, version, mixed):
    s = parsed(replace(settings, allow_mixed=True), version, mixed)
    state = s.browser_state(now=NOW)
    assert [x['domain'] for x in state['cookies']] == ['.goofish.com', 'www.goofish.com']
    assert [x['value'] for x in state['cookies']] == ['COOKIE_DUMMY', 'COOKIE_HOST_DUMMY']
    assert state['cookies'][0]['expires'] == -1 and state['cookies'][1]['expires'] == NOW + 100
    assert len(state['origins'][0]['localStorage'][0]['value']) == 118331
    assert s.observed_age_seconds == 0 and s.mixed is mixed
    assert 'COOKIE_DUMMY' not in repr(s) and 'STORAGE_DUMMY' not in repr(s)
    assert s.context_environment()['env']['screen']['width'] == 1280
    data = s.session_storage()
    assert data == {'fixture': 'STORAGE_DUMMY', '__proto__': 'not-code'}
    assert 'COOKIE_DUMMY' not in json.dumps(data)


def test_mixed_requires_opt_in_and_unspecified_is_not_silently_downgraded(settings):
    with pytest.raises(module.AssCredentialsError, match='ass_mixed_requires_opt_in'):
        parsed(settings, 2, True)
    data = bundle()
    data['cookies'][0]['sameSite'] = 'unspecified'
    with pytest.raises(module.AssCredentialsError, match='ass_cookie_samesite_unrepresentable'):
        module.parse_session(json.dumps(data), settings, now=NOW)
    result = module.parse_session(json.dumps(data), replace(settings, unspecified_same_site='lax'), now=NOW)
    assert result.browser_state(now=NOW)['cookies'][0]['sameSite'] == 'Lax'


def mutate(d, case):
    if case == 'format': d['format'] = 'ass-cookie-bundle'
    elif case == 'bool_version': d['schema_version'] = True
    elif case == 'future': d['observed_at'] = iso(NOW + 61)
    elif case == 'naive_time': d['observed_at'] = '2026-10-19T00:00:00'
    elif case == 'bad_uuid': d['producer_id'] = 'not-uuid'
    elif case == 'source': d['source_origins'] = ['https://outside.invalid']
    elif case == 'account': d['account_id'] = 'different'
    elif case == 'site': d['site_id'] = 'different'
    elif case == 'foreign_cookie': d['cookies'][0]['domain'] = '.outside.invalid'
    elif case == 'partitioned': d['cookies'][0]['partitionKey'] = 'SENSITIVE_SENTINEL'
    elif case == 'store': d['cookies'][0]['storeId'] = '1'
    elif case == 'cookie_utf8': d['cookies'][0]['value'] = '汉' * 6000
    elif case == 'cookie_control': d['cookies'][0]['value'] = 'bad\r\n'
    elif case == 'duplicate': d['cookies'].append(copy.deepcopy(d['cookies'][0]))
    elif case == 'host_only': d['cookies'][0]['hostOnly'] = True
    elif case == 'expiry': d['cookies'][0]['expirationDate'] = NOW + 1
    elif case == 'page_query': d['pages'][0]['page_url'] += '?token=SENSITIVE_SENTINEL'
    elif case == 'page_missing': d['pages'] = []
    elif case == 'referrer': d['pages'][0]['referrer'] = 'https://outside.invalid/'
    elif case == 'environment': d['pages'][0]['env']['navigator'] = {'x': 'X' * 4097}
    elif case == 'storage_key': d['pages'][0]['storage']['local'] = {'汉' * 171: 'X'}
    elif case == 'storage_count': d['pages'][0]['storage']['session'] = {str(i): 'X' for i in range(501)}
    elif case == 'header': d['pages'][0]['headers']['authorization'] = 'SENSITIVE_SENTINEL'
    elif case == 'header_unobserved': d['pages'][0]['headers_status'] = 'not_observed'
    elif case == 'header_control': d['pages'][0]['headers']['accept'] = 'bad\n'
    elif case == 'page_freshness': d['pages_observed_at'] = iso(NOW + 1)
    elif case == 'baseline': d['page_baseline_id'] = d['snapshot_id']
    elif case == 'unknown': d['unknown'] = 'SENSITIVE_SENTINEL'
    elif case == 'cookie_count': d['cookies'] += [d['cookies'][0]] * 500
    elif case == 'oversize': d['pages'][0]['storage']['local']['fixture'] = 'X' * module.MAX_SESSION_BYTES
    else: raise AssertionError(case)


@pytest.mark.parametrize('case', ['format', 'bool_version', 'future', 'naive_time', 'bad_uuid', 'source', 'account', 'site', 'foreign_cookie', 'partitioned', 'store', 'cookie_utf8', 'cookie_control', 'duplicate', 'host_only', 'expiry', 'page_query', 'page_missing', 'referrer', 'environment', 'storage_key', 'storage_count', 'header', 'header_unobserved', 'header_control', 'page_freshness', 'baseline', 'unknown', 'cookie_count', 'oversize'])
def test_strict_whole_packet_and_sanitized_errors(settings, case):
    data = bundle(2, True)
    mutate(data, case)
    with pytest.raises(module.AssCredentialsError) as exc:
        module.parse_session(json.dumps(data), replace(settings, allow_mixed=True), now=NOW)
    assert 'SENSITIVE_SENTINEL' not in str(exc.value) and 'COOKIE_DUMMY' not in str(exc.value)


def test_budget_boundary_and_expired_cookie(settings):
    data = bundle()
    data['pages'][0]['storage']['local']['fixture'] = ''
    data['pages'][0]['storage']['local']['fixture'] = 'X' * (module.MAX_SESSION_BYTES - len(json.dumps(data).encode()))
    raw = json.dumps(data)
    assert len(raw.encode()) == module.MAX_SESSION_BYTES
    module.parse_session(raw, settings, now=NOW)
    with pytest.raises(module.AssCredentialsError): module.parse_session(raw + ' ', settings, now=NOW)
    data = bundle()
    data['cookies'][1]['expirationDate'] = NOW - 1
    assert len(module.parse_session(json.dumps(data), settings, now=NOW).browser_state(now=NOW)['cookies']) == 1
    with pytest.raises(module.AssCredentialsError, match='ass_session_invalidated'):
        module.parse_session(json.dumps(data | {'state': 'invalid'}), settings, now=NOW)


def responses(settings):
    meta = {'id': SECRET, 'tenant_id': TENANT, 'name': settings.secret_name, 'current_version': 7}
    return [{'key_id': KEY, 'tenant_id': TENANT, 'value_scope_authorized': True, 'revision': 1,
             'active_grant_count': 1, 'checked_at': iso(), 'expires_at': None}, meta,
            {'name': settings.secret_name, 'version': 7, 'value': json.dumps(bundle())}, dict(meta)]


def mock_http(monkeypatch, queue, paths, requests=None):
    real = httpx.Client
    def handler(req):
        paths.append(req.url.path)
        assert req.headers['authorization'] == 'Bearer ' + TOKEN
        if requests is not None: requests.append(req)
        data = queue.pop(0)
        return httpx.Response(*data) if isinstance(data, tuple) else httpx.Response(200, json=data)
    def factory(**kwargs):
        assert kwargs['verify'] is True and kwargs['trust_env'] is False and kwargs['follow_redirects'] is False
        return real(**kwargs, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(module.httpx, 'Client', factory)
    monkeypatch.setattr(module.time, 'time', lambda: NOW)


def test_fresh_read_and_no_key_or_value_cache(settings, monkeypatch):
    queue = responses(settings) + responses(settings)
    paths = []
    mock_http(monkeypatch, queue, paths)
    client = module.AssSessionClient(settings)
    assert client.read().browser_state(now=NOW)['cookies'][0]['value'] == 'COOKIE_DUMMY'
    assert client.read().observed_age_seconds == 0
    assert len(paths) == 8 and paths[1] == '/api/v1/secrets/session.goofish.main/metadata'
    assert vars(client) == {'_settings': settings}


@pytest.mark.parametrize('index,change', [(0, {'key_id': SECRET}), (0, {'tenant_id': SECRET}), (0, {'value_scope_authorized': False}),
                                         (0, {'active_grant_count': 2}), (0, {'checked_at': iso(NOW - 61)}), (0, {'expires_at': iso(NOW - 1)}),
                                         (1, {'id': KEY}), (1, {'name': 'different'}), (1, {'current_version': True}),
                                         (2, {'version': 8}), (3, {'current_version': 8})])
def test_identity_and_version_races_stop_without_retry(settings, monkeypatch, index, change):
    queue = responses(settings)
    queue[index].update(change)
    paths = []
    mock_http(monkeypatch, queue, paths)
    with pytest.raises(module.AssCredentialsError): module.AssSessionClient(settings).read()
    assert len(paths) <= 4


@pytest.mark.parametrize('status,code', [(302, 'ass_unavailable'), (401, 'ass_authentication_rejected'), (403, 'ass_access_denied'), (404, 'ass_secret_unavailable'), (429, 'ass_rate_limited'), (503, 'ass_unavailable')])
def test_revocation_redirect_and_failure_no_replay(settings, monkeypatch, status, code):
    real = httpx.Client
    calls = []
    def handler(req):
        calls.append(req.url.path)
        return httpx.Response(status, headers={'location': 'https://outside.invalid/'})
    monkeypatch.setattr(module.httpx, 'Client', lambda **kwargs: real(**kwargs, transport=httpx.MockTransport(handler)))
    with pytest.raises(module.AssCredentialsError, match=code): module.AssSessionClient(settings).read()
    assert len(calls) == 1


def test_key_permissions_symlink_rotation_and_error_privacy(settings, monkeypatch):
    settings.key_file.chmod(0o644)
    with pytest.raises(module.AssCredentialsError, match='ass_key_file_invalid'): module.AssSessionClient(settings).read()
    settings.key_file.chmod(0o600)
    link = settings.key_file.with_name('link')
    link.symlink_to(settings.key_file)
    with pytest.raises(module.AssCredentialsError, match='ass_key_file_invalid'): module.AssSessionClient(replace(settings, key_file=link)).read()
    queue = responses(settings)
    mock_http(monkeypatch, queue, [])
    module.AssSessionClient(settings).read()
    settings.key_file.write_text('SENSITIVE_KEY_SENTINEL')
    with pytest.raises(module.AssCredentialsError) as exc: module.AssSessionClient(settings).read()
    assert 'SENSITIVE_KEY_SENTINEL' not in str(exc.value)


def test_environment_is_lazy_and_requires_complete_pins(settings, monkeypatch):
    keys = {'ASS_ORIGIN': settings.origin, 'ASS_READ_KEY_FILE': str(settings.key_file), 'ASS_TENANT_ID': TENANT,
            'ASS_SECRET_ID': SECRET, 'ASS_SECRET_NAME': settings.secret_name}
    for k, v in keys.items(): monkeypatch.setenv(k, v)
    assert module.AssSessionSettings.from_env() == settings
    monkeypatch.setenv('ASS_ALLOW_MIXED', 'yes')
    with pytest.raises(module.AssCredentialsError, match='ass_configuration_invalid'): module.AssSessionSettings.from_env()
    monkeypatch.setenv('ASS_ALLOW_MIXED', 'false')
    monkeypatch.setenv('ASS_ORIGIN', 'https://user:password@outside.invalid/')
    with pytest.raises(module.AssCredentialsError, match='ass_configuration_invalid'): module.AssSessionSettings.from_env()


def test_live_status_microseconds_and_revocation_between_reads(settings, monkeypatch):
    queue = responses(settings)
    queue[0]['checked_at'] = iso().replace('.000', '.000123')
    queue.append((403,))
    paths = []
    mock_http(monkeypatch, queue, paths)
    client = module.AssSessionClient(settings)
    assert client.read().observed_age_seconds == 0
    with pytest.raises(module.AssCredentialsError, match='ass_access_denied'): client.read()
    assert len(paths) == 5


@pytest.mark.parametrize('value_stage', [False, True])
def test_response_size_budgets_are_enforced_before_projection(settings, monkeypatch, value_stage):
    queue = responses(settings)
    real = httpx.Client
    calls = []
    def handler(req):
        calls.append(req.url.path)
        if not value_stage or req.url.path.endswith('/value'):
            size = module.MAX_RESPONSE_BYTES if value_stage else 20 * 1024
            return httpx.Response(200, content=b'X' * (size + 1))
        return httpx.Response(200, json=queue.pop(0))
    monkeypatch.setattr(module.httpx, 'Client', lambda **kwargs: real(**kwargs, transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(module.time, 'time', lambda: NOW)
    with pytest.raises(module.AssCredentialsError, match='ass_response_invalid'): module.AssSessionClient(settings).read()
    assert len(calls) == (3 if value_stage else 1)


def test_transport_errors_never_expose_request_or_key(settings, monkeypatch):
    real = httpx.Client
    def handler(req): raise httpx.ConnectError('SENSITIVE_REQUEST_SENTINEL', request=req)
    monkeypatch.setattr(module.httpx, 'Client', lambda **kwargs: real(**kwargs, transport=httpx.MockTransport(handler)))
    with pytest.raises(module.AssCredentialsError) as exc: module.AssSessionClient(settings).read()
    assert str(exc.value) == 'ass_unavailable' and exc.value.__suppress_context__
