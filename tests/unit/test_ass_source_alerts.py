"""ASS source faults are distinct from website failures; no real sends."""
import asyncio
import json
from pathlib import Path

import pytest

from test_ass_account_flow import scraper as scraper, task


@pytest.mark.parametrize('reason,title,advice', [
    ('ass_access_denied', 'ASS 读取授权异常', '读取权限'),
    ('ass_authentication_rejected', 'ASS 读取授权异常', '读取权限'),
    ('ass_unavailable', 'ASS 连接异常', '连接'),
    ('ass_session_invalid', 'ASS 会话不可用', '完整会话同步'),
])
def test_source_notice_does_not_claim_website_pause(scraper, reason, title, advice):
    s, _ = scraper
    product, body = s._failure_notice(task(), reason, phase='ASS来源读取',
                                     paused_until=None, consecutive=0)
    assert product['商品标题'] == f'{title}｜fixture'
    assert product['notification_type'] == 'task_failure'
    assert advice in body
    assert '未增加网站失败计数' in body
    assert '阶段: ASS来源读取' in body
    assert '暂停到:' not in body
    assert '连续失败:' not in body


@pytest.mark.parametrize('reason', ['ass_access_denied', 'SENSITIVE_COOKIE_OR_SCRIPT_SENTINEL'])
def test_source_failure_alert_is_throttled_without_touching_website_guard(scraper, tmp_path, monkeypatch, capsys, reason):
    s, guard = scraper
    guard.path = str(tmp_path / 'guard.json')
    original = '{"historical_website_state": true}'
    Path(guard.path).write_text(original)
    sent = []
    async def notify(product, body):
        sent.append((product, body))
    def failed():
        raise s.AssCredentialsError(reason)
    monkeypatch.setattr(s, 'send_ntfy_notification', notify)
    monkeypatch.setattr(s.AssSessionSettings, 'from_env', failed)
    for _ in range(2):
        assert asyncio.run(s.scrape_xianyu(task())) == 0
    assert len(sent) == 1
    assert not guard.failures and guard.successes == 0
    assert Path(guard.path).read_text() == original
    assert '未增加网站失败计数' in sent[0][1]
    assert 'SENSITIVE' not in str(sent) + capsys.readouterr().out


def test_cli_pause_notice_is_safe_without_reading_source(scraper, monkeypatch):
    from datetime import datetime
    from types import SimpleNamespace
    s, guard = scraper
    decision = SimpleNamespace(skip=True, should_notify=True, consecutive_failures=3,
                               paused_until=datetime.fromisoformat('2026-11-14T10:00:00+08:00'),
                               reason='site_login_required')
    monkeypatch.setattr(guard, 'should_skip_start', lambda *a, **k: decision)
    monkeypatch.setattr(s.AssSessionSettings, 'from_env',
                        lambda: (_ for _ in ()).throw(AssertionError('source-read-forbidden')))
    sent = []
    async def notify(product, body): sent.append((product, body))
    monkeypatch.setattr(s, 'send_ntfy_notification', notify)
    assert asyncio.run(s.scrape_xianyu(task())) == 0
    assert sent[0][0]['notification_type'] == 'task_failure'
    assert '阶段: 暂停检查' in sent[0][1]
    assert '完整会话同步' in sent[0][1]


def test_parent_pause_notice_uses_safe_actionable_wording(monkeypatch, tmp_path, capsys):
    from datetime import datetime
    from types import SimpleNamespace
    from src.services.process_service import ProcessService
    service = ProcessService()
    sent = []
    async def notify(product, body): sent.append((product, body))
    monkeypatch.setattr('src.services.process_service.send_ntfy_notification', notify)
    decision = SimpleNamespace(should_notify=True, consecutive_failures=3,
                               paused_until=datetime.fromisoformat('2026-11-14T10:00:00+08:00'),
                               reason='SENSITIVE_COOKIE_OR_SCRIPT_SENTINEL')
    asyncio.run(service._notify_skip('fixture', decision))
    assert sent[0][0]['notification_type'] == 'task_failure'
    assert '阶段: 暂停检查' in sent[0][1]
    assert 'SENSITIVE' not in str(sent) + capsys.readouterr().out
