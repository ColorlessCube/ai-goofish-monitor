"""来源选择、父子暂停与浏览器内存恢复；不访问真实ASS、闲鱼、AI或通知。"""
import asyncio
import importlib
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.failure_guard import FailureGuard
from src.services.process_service import ProcessService
from src.services.account_strategy_service import resolve_account_runtime_plan
from test_ass_session_client import parsed, settings as settings


def test_task_ass_roundtrip_and_partial_update_never_falls_back(api_client, sample_task_payload):
    payload = sample_task_payload | {'account_strategy': 'ass', 'account_state_file': 'must-not-be-used.json'}
    r = api_client.post('/api/tasks/', json=payload)
    assert r.status_code == 200
    assert r.json()['task']['account_strategy'] == 'ass'
    assert r.json()['task']['account_state_file'] is None
    r = api_client.patch('/api/tasks/0', json={'max_pages': 4})
    assert r.status_code == 200
    assert r.json()['task']['account_strategy'] == 'ass'
    assert r.json()['task']['account_state_file'] is None
    assert 'ASS_READ_KEY_FILE' not in json.dumps(r.json())
    r = api_client.patch('/api/tasks/0', json={'account_state_file': None})
    assert r.status_code == 200 and r.json()['task']['account_strategy'] == 'ass'
    r = api_client.post('/api/tasks/generate', json={'task_name': 'ass-keyword', 'keyword': 'fixture', 'decision_mode': 'keyword', 'keyword_rules': ['fixture'], 'account_strategy': 'ass'})
    assert r.status_code == 200 and r.json()['task']['account_strategy'] == 'ass'


def test_runtime_plan_and_parent_do_not_use_local_mtime_for_ass(tmp_path, monkeypatch):
    local = tmp_path / 'state.json'
    local.write_text('{}')
    monkeypatch.setattr('src.services.process_service.STATE_FILE', str(local))
    monkeypatch.setattr('src.services.process_service.find_task_by_name_sync', lambda name: SimpleNamespace(account_strategy='ass', account_state_file=str(local)))
    service = ProcessService()
    service.failure_guard = FailureGuard(str(tmp_path / 'guard.json'), threshold=1)
    service.failure_guard.record_failure('fixture', 'site_risk_control', cookie_path=str(local))
    local.touch()
    assert service._resolve_cookie_path('fixture') is None
    assert service.failure_guard.should_skip_start('fixture', cookie_path=service._resolve_cookie_path('fixture')).skip
    plan = resolve_account_runtime_plan(strategy='ass', account_state_file=str(local), has_root_state_file=True, available_account_files=[str(local)])
    assert not plan['prefer_root_state'] and not plan['use_account_pool'] and plan['forced_account'] is None


def test_cli_ass_without_any_local_state(tmp_path, monkeypatch):
    fake = types.ModuleType('src.scraper')
    calls = []
    async def scrape(task_config, debug_limit):
        calls.append(task_config); return 0
    fake.scrape_xianyu = scrape
    monkeypatch.setitem(sys.modules, 'src.scraper', fake)
    monkeypatch.delitem(sys.modules, 'spider_v2', raising=False)
    cli = importlib.import_module('spider_v2')
    monkeypatch.setattr(cli, 'STATE_FILE', str(tmp_path / 'missing.json'))
    monkeypatch.setenv('ACCOUNT_STATE_DIR', str(tmp_path / 'none'))
    p = tmp_path / 'config.json'
    p.write_text(json.dumps([{'task_name': 'ass', 'enabled': True, 'keyword': 'fixture', 'decision_mode': 'keyword', 'keyword_rules': ['fixture'], 'account_strategy': 'ass'}]))
    monkeypatch.setattr(sys, 'argv', ['spider_v2.py', '--config', str(p), '--task-name', 'ass'])
    asyncio.run(cli.main())
    assert len(calls) == 1 and calls[0]['account_strategy'] == 'ass'


class Guard:
    threshold = 3
    def __init__(self, skip=False):
        self.paths = []; self.failures = []; self.successes = 0; self.skip = skip
    def should_skip_start(self, _name, *, cookie_path):
        self.paths.append(cookie_path)
        return SimpleNamespace(skip=self.skip, should_notify=False, consecutive_failures=3)
    def record_failure(self, _name, reason, *, cookie_path, **_kwargs):
        self.failures.append((reason, cookie_path))
        return {'should_notify': False, 'consecutive_failures': 1}
    def record_success(self, _name):
        self.successes += 1


@pytest.fixture
def scraper(monkeypatch, tmp_path):
    s = importlib.import_module('src.scraper')
    monkeypatch.setattr(s, 'load_price_snapshots', lambda *a: [])
    monkeypatch.setattr(s, 'load_processed_link_keys', lambda *a: set())
    monkeypatch.setattr(s, 'cleanup_task_images', lambda *a: None)
    monkeypatch.setattr(s, 'load_state_files', lambda *a: (_ for _ in ()).throw(AssertionError('local-pool-forbidden')))
    local = tmp_path / 'local.json'; local.write_text('{}')
    monkeypatch.setattr(s, 'STATE_FILE', str(local))
    guard = Guard(); monkeypatch.setattr(s, 'FAILURE_GUARD', guard)
    return s, guard


def task():
    return {'task_name': 'fixture', 'keyword': 'fixture', 'account_strategy': 'ass', 'proxy_rotation': {'enabled': False}, 'account_rotation': {'enabled': True}}


def test_ass_source_failure_does_not_touch_website_guard_or_fallback(scraper, monkeypatch):
    s, guard = scraper
    def failed(): raise s.AssCredentialsError('ass_access_denied')
    monkeypatch.setattr(s.AssSessionSettings, 'from_env', failed)
    monkeypatch.setattr(s, 'async_playwright', lambda: (_ for _ in ()).throw(AssertionError('browser-forbidden')))
    assert asyncio.run(s.scrape_xianyu(task())) == 0
    assert guard.paths == [None] and not guard.failures and not guard.successes


def test_pause_blocks_remote_reads_even_with_new_snapshot_or_changed_local_file(scraper, monkeypatch):
    s, guard = scraper; guard.skip = True
    monkeypatch.setattr(s.AssSessionSettings, 'from_env', lambda: (_ for _ in ()).throw(AssertionError('credential-read-forbidden')))
    assert asyncio.run(s.scrape_xianyu(task())) == 0
    assert guard.paths == [None] and not guard.successes


def test_browser_setup_errors_never_echo_storage_or_cookie_payload(monkeypatch, settings):
    s = importlib.import_module('src.scraper')
    session = parsed(settings)
    class Browser:
        async def new_context(self, **kwargs):
            assert kwargs['storage_state']['origins']
            raise RuntimeError('SENSITIVE_COOKIE_OR_SCRIPT_SENTINEL')
    with pytest.raises(s.AssCredentialsError, match='^ass_browser_setup_failed$') as exc:
        asyncio.run(s._new_ass_context(Browser(), session))
    assert 'SENSITIVE' not in str(exc.value) and exc.value.__suppress_context__


@pytest.mark.parametrize('kind', ['login', 'risk'])
def test_site_login_or_risk_uses_no_cookie_path_and_no_rotation(scraper, settings, monkeypatch, capsys, kind):
    s, guard = scraper; session = parsed(settings)
    monkeypatch.setattr(s.AssSessionSettings, 'from_env', lambda: settings)
    monkeypatch.setattr(s, 'AssSessionClient', lambda _: SimpleNamespace(read=lambda: session))
    launches = []
    async def no_delay(*args): pass
    class Page:
        url = 'https://www.goofish.com/'
        async def route(self, *a): pass
        async def unroute(self, *a): pass
        async def evaluate(self, *a): pass
        async def goto(self, url, **k):
            if url.endswith('/__ass_session_seed__'): return
            raise (s.LoginRequiredError if kind == 'login' else s.RiskControlError)('SENSITIVE_COOKIE_OR_SCRIPT_SENTINEL')
    class Context:
        async def add_init_script(self, **kwargs): pass
        async def new_page(self): return Page()
    class Browser:
        async def new_context(self, **kwargs): return Context()
        async def close(self): pass
    class Chromium:
        async def launch(self, **kwargs): launches.append(kwargs); return Browser()
    class Playwright:
        async def __aenter__(self): return SimpleNamespace(chromium=Chromium())
        async def __aexit__(self, *a): pass
    class Dispatcher:
        def __init__(self, **kwargs): pass
        async def join(self): pass
    monkeypatch.setattr(s, 'async_playwright', Playwright)
    monkeypatch.setattr(s, 'ItemAnalysisDispatcher', Dispatcher)
    monkeypatch.setattr(s, 'random_sleep', no_delay)
    monkeypatch.setattr(s.asyncio, 'sleep', no_delay)
    assert asyncio.run(s.scrape_xianyu(task())) == 0
    assert len(launches) == 1 and guard.successes == 0
    assert '--disable-web-security' not in launches[0]['args']
    assert '--disable-features=IsolateOrigins,site-per-process' not in launches[0]['args']
    assert guard.failures == [('site_login_required' if kind == 'login' else 'site_risk_control', None)]
    assert 'SENSITIVE_COOKIE_OR_SCRIPT_SENTINEL' not in capsys.readouterr().out


@pytest.mark.parametrize('ret,kind', [(['FAIL_SYS_USER_VALIDATE::SENSITIVE'], 'risk'), (['FAIL_SYS_SESSION_EXPIRED::SENSITIVE'], 'login'), (['FAIL_SYS_USER_NOT_LOGIN::SENSITIVE'], 'login')])
def test_ass_website_denials_are_not_successful_empty_results(ret, kind):
    s = importlib.import_module('src.scraper')
    with pytest.raises(s.RiskControlError if kind == 'risk' else s.LoginRequiredError) as exc:
        s._check_ass_site_response({'ret': ret})
    assert 'SENSITIVE' not in str(exc.value)
    s._check_ass_site_response({'ret': ['SUCCESS::调用成功'], 'data': {'items': []}})
