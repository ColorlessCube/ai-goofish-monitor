"""Exercise real CLI main + real scraper at offline I/O boundaries."""
import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from test_ass_account_flow import scraper as scraper, task
from test_ass_session_client import settings as settings
from test_failure_diagnosis import browser_run as browser_run


@pytest.fixture
def cli_main(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location('cli_outcome_fixture', Path(__file__).parents[2] / 'spider_v2.py')
    assert spec is not None and spec.loader is not None
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    config = tmp_path / 'tasks.json'
    config.write_text(json.dumps([task() | {'enabled': True, 'decision_mode': 'keyword', 'max_pages': 1}]))
    monkeypatch.setattr(sys, 'argv', ['spider_v2.py', '--config', str(config)])
    return cli.main


@pytest.mark.parametrize('raw_reason,safe_reason', [
    ('ass_access_denied', 'ass_access_denied'),
    ('SENSITIVE_COOKIE token=SECRET', 'ass_source_failed'),
])
def test_real_cli_main_reports_real_source_failure(scraper, cli_main, monkeypatch, tmp_path, capsys, raw_reason, safe_reason):
    s, guard = scraper
    guard.path = str(tmp_path / 'guard.json')
    notices = []
    async def notify(product, body): notices.append((product, body))
    def unavailable(): raise s.AssCredentialsError(raw_reason)
    monkeypatch.setattr(s.AssSessionSettings, 'from_env', unavailable)
    monkeypatch.setattr(s, 'send_ntfy_notification', notify)
    monkeypatch.setattr(s, 'async_playwright', lambda: pytest.fail('source fault must not launch browser'))
    asyncio.run(cli_main())
    output = capsys.readouterr().out
    final = output.split('--- 所有任务执行完毕 ---')[-1]
    assert '来源不可用' in final
    assert safe_reason in final
    assert '正常结束' not in final
    assert '0 个新商品' in final
    assert 'SENSITIVE' not in output and 'SECRET' not in output
    assert not guard.failures and guard.successes == 0
    assert len(notices) == 1


@pytest.mark.parametrize('kind,label,reason', [
    ('paused', '已暂停', None),
    ('login', '执行失败', 'site_login_required'),
    ('risk', '执行失败', 'site_risk_control'),
    ('timeout', '执行失败', 'browser_timeout'),
    ('success', '正常结束', None),
])
def test_real_cli_main_distinguishes_terminal_outcomes(browser_run, cli_main, capsys, kind, label, reason):
    s, guard, state, _ = browser_run
    if kind == 'paused': guard.skip = True
    if kind == 'login': state.ret = ['FAIL_SYS_SESSION_EXPIRED::SENSITIVE']
    if kind == 'risk': state.ret = ['FAIL_SYS_USER_VALIDATE::SENSITIVE']

    # A navigation timeout exercises the outer browser boundary, not a fake result.
    if kind == 'timeout':
        async def timeout(*args, **kwargs): raise s.PlaywrightTimeoutError('SENSITIVE_TIMEOUT')
        browser_run[3].goto = timeout
    asyncio.run(cli_main())
    output = capsys.readouterr().out
    final = output.split('--- 所有任务执行完毕 ---')[-1]
    assert label in final
    if kind != 'success': assert '正常结束' not in final
    if reason: assert reason in final
    assert '0 个新商品' in final
    assert 'SENSITIVE' not in output
    assert guard.successes == (1 if kind == 'success' else 0)


def test_real_cli_main_redacts_unexpected_ass_exception(scraper, cli_main, monkeypatch, capsys):
    s, _ = scraper
    def fail(*args): raise RuntimeError('SENSITIVE_COOKIE token=SECRET')
    monkeypatch.setattr(s, 'load_price_snapshots', fail)
    asyncio.run(cli_main())
    output = capsys.readouterr().out
    assert 'SENSITIVE' not in output and 'SECRET' not in output
    final = output.split('--- 所有任务执行完毕 ---')[-1]
    assert '异常而终止' in final and 'browser_operation_failed' in final
    assert '正常结束' not in final


@pytest.mark.parametrize('kind,label', [('source', '来源不可用'), ('login', '执行失败'), ('risk', '执行失败')])
def test_real_cli_retains_count_before_later_failure(browser_run, cli_main, monkeypatch, tmp_path, capsys, kind, label):
    from types import SimpleNamespace
    s, guard, state, page = browser_run
    guard.path = str(tmp_path / 'guard.json')
    submitted = []
    class Dispatcher:
        def __init__(self, **kwargs): pass
        def submit(self, job): submitted.append(job)
        async def join(self): pass
    async def parse(*args):
        return [{'商品链接': f'https://www.goofish.com/item?id={i}', '商品标题': f'fixture {i}'} for i in (1, 2)]
    visits = []
    async def detail_goto(*args, **kwargs):
        visits.append(True)
        if len(visits) == 2:
            error = {'source': s.AssCredentialsError, 'login': s.LoginRequiredError, 'risk': s.RiskControlError}[kind]
            raise error('SENSITIVE_LATER_FAILURE')
    async def close(): pass
    pages = []
    async def new_page(*args):
        pages.append(True)
        if len(pages) == 1: return page
        return SimpleNamespace(expect_response=page.expect_response, goto=detail_goto, close=close)
    monkeypatch.setattr(s, 'ItemAnalysisDispatcher', Dispatcher)
    monkeypatch.setattr(s, '_parse_search_results_json', parse)
    monkeypatch.setattr(s, '_new_task_page', new_page)
    monkeypatch.setattr(s, 'record_market_snapshots', lambda **kwargs: [])
    monkeypatch.setattr(s, 'build_market_reference', lambda **kwargs: {})
    asyncio.run(cli_main())
    output = capsys.readouterr().out
    final = output.split('--- 所有任务执行完毕 ---')[-1]
    assert len(submitted) == 1
    assert label in final and '1 个新商品' in final
    assert '正常结束' not in final and 'SENSITIVE' not in output
    assert guard.successes == 0


@pytest.mark.parametrize('status', ['success', 'source_unavailable', 'paused', 'failed'])
def test_scrape_result_preserves_numeric_contract_and_safe_metadata(status):
    from src.services.scrape_result import ScrapeResult
    result = ScrapeResult(7, status=status, reason='SENSITIVE_TOKEN')
    assert isinstance(result, int) and result == 7 and result > 0
    assert str(result) == '7' and json.dumps(result) == '7'
    assert result.status == status
    assert 'SENSITIVE' not in result.reason
    with pytest.raises(ValueError, match='^invalid_scrape_status$'):
        ScrapeResult(7, status='invalid')
