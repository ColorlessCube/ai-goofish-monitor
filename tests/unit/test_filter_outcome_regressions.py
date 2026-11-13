"""Configured filters must be confirmed, not silently reuse an old response."""
import asyncio

import pytest

from test_ass_account_flow import scraper as scraper, task
from test_ass_session_client import settings as settings
from test_failure_diagnosis import browser_run as browser_run


@pytest.mark.parametrize('overlay,reason', [
    ('mini_login', 'site_login_required'),
    ('baxia-dialog', 'site_risk_control'),
    (None, 'browser_timeout'),
])
def test_region_inner_submit_timeout_stops_stale_response(browser_run, monkeypatch, capsys, overlay, reason):
    s, guard, state, page = browser_run
    original_locator = page.locator

    class RegionLocator:
        def __init__(self, selector): self.selector = selector
        @property
        def first(self): return self
        @property
        def last(self): return self
        def filter(self, **kwargs): return self
        def locator(self, selector, **kwargs): return RegionLocator(selector)
        def nth(self, index): return self
        async def count(self): return 1
        async def wait_for(self, **kwargs): pass
        async def click(self):
            state.clicks.append(self.selector)
            if 'searchBtn' in self.selector:
                state.overlay = overlay
                raise s.PlaywrightTimeoutError('SENSITIVE_REGION_COOKIE token=SECRET')

    monkeypatch.setattr(page, 'get_by_text', lambda text, **kwargs: RegionLocator(text), raising=False)
    monkeypatch.setattr(page, 'locator', lambda selector: (
        RegionLocator(selector) if selector == 'div.ant-popover' else original_locator(selector)
    ))
    result = asyncio.run(s.scrape_xianyu(task() | {'region': '浙江/杭州', 'max_pages': 1}))
    assert result == 0
    assert any('searchBtn' in click for click in state.clicks)
    assert guard.failures == [(reason, None)]
    assert guard.successes == 0
    assert state.events.count('initial_json') == sum('searchBtn' in click for click in state.clicks)
    output = capsys.readouterr().out
    assert 'SENSITIVE' not in output and 'SECRET' not in output


@pytest.mark.parametrize('config,click', [
    ({'new_publish_option': '1天内'}, 'text=新发布'),
    ({'free_shipping': True}, 'text=包邮'),
])
def test_unexplained_filter_timeout_does_not_consume_initial_response(browser_run, capsys, config, click):
    s, guard, state, _ = browser_run
    result = asyncio.run(s.scrape_xianyu(task() | config | {'max_pages': 1}))
    assert result == 0
    assert guard.failures == [('browser_timeout', None)]
    assert guard.successes == 0
    # One initial JSON check per fresh attempt; none reused as the filtered list.
    assert state.events.count('initial_json') == state.clicks.count(click)
    output = capsys.readouterr().out
    assert 'SENSITIVE' not in output and 'SECRET' not in output


@pytest.mark.parametrize('config', [
    {'new_publish_option': '1天内'}, {'free_shipping': True}, {'region': '浙江/杭州'},
])
@pytest.mark.parametrize('overlay,reason', [
    (None, 'browser_timeout'), ('mini_login', 'site_login_required'),
    ('baxia-dialog', 'site_risk_control'),
])
def test_unconfirmed_filter_operation_error_rechecks_access(browser_run, monkeypatch, config, overlay, reason):
    s, guard, state, page = browser_run
    original_click = page.click
    async def broken_click(selector, **kwargs):
        if 'closeIconBg' in selector: return await original_click(selector, **kwargs)
        state.overlay = overlay
        raise RuntimeError('SENSITIVE_UNCONFIRMED_FILTER')
    def broken_region(*args, **kwargs):
        state.overlay = overlay
        raise RuntimeError('SENSITIVE_UNCONFIRMED_FILTER')
    monkeypatch.setattr(page, 'click', broken_click)
    monkeypatch.setattr(page, 'get_by_text', broken_region, raising=False)
    asyncio.run(s.scrape_xianyu(task() | config))
    assert guard.failures == [(reason, None)]
    assert guard.successes == 0


@pytest.mark.parametrize('missing', ['trigger', 'popover', 'option', 'submit', None])
def test_region_controls_require_confirmation(browser_run, monkeypatch, missing):
    s, guard, state, page = browser_run
    original_locator = page.locator
    class RegionLocator:
        def __init__(self, selector): self.selector = selector
        @property
        def first(self): return self
        @property
        def last(self): return self
        def filter(self, **kwargs): return self
        def locator(self, selector, **kwargs): return RegionLocator(selector)
        def nth(self, index): return self
        async def count(self):
            absent = {'trigger': '区域', 'popover': 'div.ant-popover',
                      'option': '.provItem--QAdOx8nD', 'submit': 'div.searchBtn--Ic6RKcAb'}
            return 0 if self.selector == absent.get(missing) else 1
        async def wait_for(self, **kwargs): pass
        async def click(self): state.clicks.append(self.selector)
    monkeypatch.setattr(page, 'get_by_text', lambda text, **kwargs: RegionLocator(text), raising=False)
    monkeypatch.setattr(page, 'locator', lambda selector: (
        RegionLocator(selector) if selector == 'div.ant-popover' else original_locator(selector)
    ))
    asyncio.run(s.scrape_xianyu(task() | {'region': '浙江/杭州'}))
    assert guard.failures == ([('browser_timeout', None)] if missing else [])
    assert guard.successes == (0 if missing else 1)


@pytest.mark.parametrize('config', [{'new_publish_option': '1天内'}, {'free_shipping': True}])
def test_invalid_filtered_response_cannot_fall_back_to_initial(browser_run, monkeypatch, config):
    s, guard, state, page = browser_run
    original_expect = page.expect_response
    original_click = page.click
    class InvalidExpected:
        async def __aenter__(self):
            async def invalid():
                from types import SimpleNamespace
                return SimpleNamespace(ok=False)
            self.value = invalid()
            return self
        async def __aexit__(self, *args):
            if args[0]: self.value.close()
    calls = []
    def expect(*args, **kwargs):
        calls.append(True)
        return original_expect(*args, **kwargs) if len(calls) % 2 else InvalidExpected()
    async def click(selector, **kwargs):
        if 'closeIconBg' in selector: return await original_click(selector, **kwargs)
    monkeypatch.setattr(page, 'expect_response', expect)
    monkeypatch.setattr(page, 'click', click)
    asyncio.run(s.scrape_xianyu(task() | config))
    assert guard.failures == [('browser_timeout', None)]
    assert guard.successes == 0


@pytest.mark.parametrize('config', [{'new_publish_option': '1天内'}, {'free_shipping': True}])
def test_filter_response_wait_timeout_does_not_reuse_initial(browser_run, monkeypatch, config):
    s, guard, state, page = browser_run
    original_expect = page.expect_response
    original_click = page.click
    class TimeoutExpected:
        async def __aenter__(self): return self
        async def __aexit__(self, *args):
            raise s.PlaywrightTimeoutError('SENSITIVE_RESPONSE_WAIT')
    calls = []
    def expect(*args, **kwargs):
        calls.append(True)
        return original_expect(*args, **kwargs) if len(calls) % 2 else TimeoutExpected()
    async def click(selector, **kwargs):
        if 'closeIconBg' in selector: return await original_click(selector, **kwargs)
    monkeypatch.setattr(page, 'expect_response', expect)
    monkeypatch.setattr(page, 'click', click)
    asyncio.run(s.scrape_xianyu(task() | config))
    assert guard.failures == [('browser_timeout', None)]
    assert guard.successes == 0


@pytest.mark.parametrize('config', [{'new_publish_option': '1天内'}, {'free_shipping': True}])
def test_confirmed_filter_response_still_succeeds(browser_run, monkeypatch, config):
    s, guard, state, page = browser_run
    original_click = page.click
    async def click(selector, **kwargs):
        if 'closeIconBg' in selector: return await original_click(selector, **kwargs)
    monkeypatch.setattr(page, 'click', click)
    result = asyncio.run(s.scrape_xianyu(task() | config))
    assert isinstance(result, int) and result == 0 and result.status == 'success'
    assert guard.failures == [] and guard.successes == 1
    assert state.events.count('initial_json') == 2


@pytest.mark.parametrize('config', [
    {'new_publish_option': '1天内'}, {'free_shipping': True}, {'region': '浙江/杭州'},
])
@pytest.mark.parametrize('kind,reason', [('login', 'site_login_required'), ('risk', 'site_risk_control')])
def test_classified_filter_fault_is_not_swallowed_by_broad_handler(browser_run, monkeypatch, config, kind, reason):
    s, guard, state, page = browser_run
    original_click = page.click
    def fault():
        error = s.LoginRequiredError if kind == 'login' else s.RiskControlError
        raise error('SENSITIVE_CLASSIFIED_FAULT')
    async def click(selector, **kwargs):
        if 'closeIconBg' in selector: return await original_click(selector, **kwargs)
        fault()
    def region(*args, **kwargs): fault()
    monkeypatch.setattr(page, 'click', click)
    monkeypatch.setattr(page, 'get_by_text', region, raising=False)
    asyncio.run(s.scrape_xianyu(task() | config))
    assert guard.failures == [(reason, None)]
    assert guard.successes == 0
