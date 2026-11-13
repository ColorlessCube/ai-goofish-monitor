"""Offline failures at the real scrape boundary; never contact ASS or a site."""
import asyncio
from types import SimpleNamespace

import pytest

from test_ass_account_flow import scraper as scraper, task
from test_ass_session_client import parsed, settings as settings


@pytest.fixture
def browser_run(scraper, settings, monkeypatch):
    s, guard = scraper
    session = parsed(settings)
    monkeypatch.setattr(s.AssSessionSettings, 'from_env', lambda: settings)
    monkeypatch.setattr(s, 'AssSessionClient', lambda _: SimpleNamespace(read=lambda: session))
    state = SimpleNamespace(ret=['SUCCESS::ok'], clicks=[], overlay=None, timeout=None,
                            diagnostics_broken=False, json_error=False, events=[])

    class Response:
        ok = True
        async def json(self):
            state.events.append('initial_json')
            if state.json_error:
                raise ValueError('SENSITIVE_CHALLENGE_BODY')
            return {'ret': state.ret, 'data': {}}

    class Expected:
        async def __aenter__(self):
            async def response(): return Response()
            self.value = response()
            return self
        async def __aexit__(self, *args):
            if args[0]: self.value.close()

    class Locator:
        def __init__(self, selector): self.selector = selector
        @property
        def first(self): return self
        async def is_visible(self):
            if state.diagnostics_broken: raise RuntimeError('SENSITIVE_DIAGNOSTIC')
            return bool(state.overlay and state.overlay in self.selector)
        async def wait_for(self, **kwargs): raise s.PlaywrightTimeoutError('SENSITIVE_OVERLAY')

    class Page:
        url = 'https://www.goofish.com/search?q=fixture'
        async def evaluate(self, *args): pass
        async def goto(self, *args, **kwargs): pass
        async def wait_for_selector(self, *args, **kwargs):
            state.events.append('wait_selector')
        def expect_response(self, *args, **kwargs): return Expected()
        def locator(self, selector): return Locator(selector)
        async def click(self, selector, **kwargs):
            state.clicks.append(selector)
            if selector == "div[class*='closeIconBg']":
                raise s.PlaywrightTimeoutError('SENSITIVE_AD')
            if state.timeout:
                state.overlay = state.timeout
            raise s.PlaywrightTimeoutError('SENSITIVE_COOKIE https://example.invalid/?token=SECRET')

    page = Page()
    class Browser:
        async def close(self): pass
    class Chromium:
        async def launch(self, **kwargs): return Browser()
    class Playwright:
        async def __aenter__(self): return SimpleNamespace(chromium=Chromium())
        async def __aexit__(self, *args): pass
    class Dispatcher:
        def __init__(self, **kwargs): pass
        async def join(self): pass
    async def context(*args): return object()
    async def new_page(*args): return page
    async def no_delay(*args): pass
    async def no_notification(*args): pass
    monkeypatch.setattr(s, 'async_playwright', Playwright)
    monkeypatch.setattr(s, '_new_ass_context', context)
    monkeypatch.setattr(s, '_new_task_page', new_page)
    monkeypatch.setattr(s, 'ItemAnalysisDispatcher', Dispatcher)
    monkeypatch.setattr(s, 'random_sleep', no_delay)
    monkeypatch.setattr(s.asyncio, 'sleep', no_delay)
    monkeypatch.setattr(s, 'send_ntfy_notification', no_notification)
    return s, guard, state, page


@pytest.mark.parametrize('ret,reason', [
    ('FAIL_SYS_SESSION_EXPIRED::SENSITIVE', 'site_login_required'),
    ('FAIL_SYS_USER_NOT_LOGIN::SENSITIVE', 'site_login_required'),
    ('FAIL_SYS_USER_VALIDATE::https://example.invalid/?token=SECRET', 'site_risk_control'),
])
def test_initial_api_denial_stops_before_filter_clicks(browser_run, capsys, ret, reason):
    s, guard, state, _ = browser_run
    state.ret = [ret]
    assert asyncio.run(s.scrape_xianyu(task() | {'personal_only': True})) == 0
    assert guard.failures == [(reason, None)]
    assert state.clicks == []
    assert state.events == ['initial_json']
    output = capsys.readouterr().out
    assert 'SENSITIVE' not in output and 'SECRET' not in output


@pytest.mark.parametrize('overlay,reason', [
    ('mini_login', 'site_login_required'),
    ('baxia-dialog', 'site_risk_control'),
    ('J_MIDDLEWARE_FRAME_WIDGET', 'site_risk_control'),
])
def test_visible_overlay_on_unchanged_search_url_stops_before_filters(browser_run, overlay, reason):
    s, guard, state, _ = browser_run
    state.overlay = overlay
    asyncio.run(s.scrape_xianyu(task() | {'personal_only': True}))
    assert guard.failures == [(reason, None)]
    assert state.clicks == []


@pytest.mark.parametrize('overlay,reason', [
    ('mini_login', 'site_login_required'), ('baxia-dialog', 'site_risk_control'),
])
def test_visible_challenge_is_classified_even_when_response_is_not_json(browser_run, overlay, reason, capsys):
    s, guard, state, _ = browser_run
    state.overlay = overlay
    state.json_error = True
    asyncio.run(s.scrape_xianyu(task()))
    assert guard.failures == [(reason, None)]
    assert state.clicks == []
    assert 'SENSITIVE' not in capsys.readouterr().out


@pytest.mark.parametrize('config', [
    {'new_publish_option': '1天内', 'personal_only': True},
    {'personal_only': True},
    {'free_shipping': True},
])
@pytest.mark.parametrize('overlay,reason', [
    ('mini_login', 'site_login_required'), ('baxia-dialog', 'site_risk_control'),
])
def test_filter_timeout_rechecks_visible_access_evidence(browser_run, config, overlay, reason, capsys):
    s, guard, state, _ = browser_run
    state.timeout = overlay
    asyncio.run(s.scrape_xianyu(task() | config))
    assert guard.failures == [(reason, None)]
    assert len(state.clicks) == 2  # ad close + first filter; no retry or next filter
    assert state.events.count('initial_json') == 1
    assert guard.successes == 0
    assert 'SENSITIVE' not in capsys.readouterr().out


@pytest.mark.parametrize('broken_diagnostics', [False, True])
def test_unexplained_timeout_is_not_cookie_expiry(browser_run, broken_diagnostics, capsys):
    s, guard, state, _ = browser_run
    state.diagnostics_broken = broken_diagnostics
    asyncio.run(s.scrape_xianyu(task() | {'personal_only': True}))
    assert guard.failures == [('browser_timeout', None)]
    assert guard.successes == 0
    output = capsys.readouterr().out
    assert 'SENSITIVE' not in output and 'SECRET' not in output


@pytest.mark.parametrize('reason,title,advice', [
    ('site_login_required', '闲鱼登录失效', '完整会话同步'),
    ('site_risk_control', '闲鱼需要验证', '完成验证'),
    ('browser_timeout', '闲鱼操作超时', '不能确定登录失效'),
    ('SENSITIVE_COOKIE https://example.invalid/?token=SECRET', '闲鱼任务失败', '原因未确定'),
])
def test_failure_notice_is_actionable_and_waits_for_three_failures(scraper, tmp_path, monkeypatch, reason, title, advice):
    from src.failure_guard import FailureGuard
    s, _ = scraper
    guard = FailureGuard(str(tmp_path / 'guard.json'))
    monkeypatch.setattr(s, 'FAILURE_GUARD', guard)
    sent = []
    async def notify(product, body): sent.append((product, body))
    monkeypatch.setattr(s, 'send_ntfy_notification', notify)
    for index in range(3):
        asyncio.run(s._notify_task_failure(task(), reason, cookie_path=None))
        assert len(sent) == (1 if index == 2 else 0)
    product, body = sent[0]
    assert product['商品标题'] == f'{title}｜fixture'
    assert product['notification_type'] == 'task_failure'
    assert '任务: fixture' in body and '阶段: 任务执行' in body and '暂停到:' in body
    assert advice in body
    assert '同步本身不会解除已有暂停' in body
    combined = str(sent) + (tmp_path / 'guard.json').read_text()
    assert 'SENSITIVE' not in combined and 'SECRET' not in combined


@pytest.mark.parametrize('ret,config,phase', [
    (['FAIL_SYS_USER_VALIDATE::SENSITIVE'], {}, '初始搜索响应'),
    (['SUCCESS::ok'], {'personal_only': True}, '筛选：个人闲置'),
])
def test_runtime_notice_identifies_actual_failure_phase(browser_run, tmp_path, monkeypatch, ret, config, phase):
    from src.failure_guard import FailureGuard
    s, _, state, _ = browser_run
    state.ret = ret
    monkeypatch.setattr(s, 'FAILURE_GUARD', FailureGuard(str(tmp_path / 'guard.json'), threshold=1))
    sent = []
    async def notify(product, body): sent.append(body)
    monkeypatch.setattr(s, 'send_ntfy_notification', notify)
    asyncio.run(s.scrape_xianyu(task() | config))
    assert len(sent) == 1
    assert f'阶段: {phase}' in sent[0]


@pytest.mark.parametrize('boundary,phase', [('launch', '浏览器启动'), ('list', '商品列表')])
def test_later_phases_do_not_report_stale_source_or_filter_stage(browser_run, tmp_path, monkeypatch, boundary, phase):
    from src.failure_guard import FailureGuard
    s, _, _, _ = browser_run
    monkeypatch.setattr(s, 'FAILURE_GUARD', FailureGuard(str(tmp_path / 'guard.json'), threshold=1))
    if boundary == 'launch':
        def fail_launch(): raise RuntimeError('SENSITIVE_LAUNCH')
        monkeypatch.setattr(s, '_resolve_browser_channel', fail_launch)
    else:
        async def fail_list(*args): raise s.LoginRequiredError('SENSITIVE_LIST')
        monkeypatch.setattr(s, '_parse_search_results_json', fail_list)
    sent = []
    async def notify(product, body): sent.append(body)
    monkeypatch.setattr(s, 'send_ntfy_notification', notify)
    asyncio.run(s.scrape_xianyu(task() | {'max_pages': 1}))
    assert len(sent) == 1
    assert f'阶段: {phase}' in sent[0]
