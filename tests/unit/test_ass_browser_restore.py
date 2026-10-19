"""可选真实Chrome离线投影；所有导航由本地route应答，不访问网站或使用真实凭据。"""
import asyncio
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from src.scraper import _new_ass_context, _new_task_page
from test_ass_session_client import parsed, settings as settings

CHROME = next((p for p in [Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'), Path('/usr/bin/google-chrome'), Path('/usr/bin/chromium')] if p.exists()), None)


@pytest.mark.skipif(CHROME is None, reason='Optional native Chrome not installed; no download required')
def test_native_restore_storage_is_origin_scoped_and_not_replayed_on_navigation(settings):
    async def run():
        session = parsed(settings)
        async with async_playwright() as p:
            browser = await p.chromium.launch(executable_path=str(CHROME), headless=True,
                                              args=['--no-sandbox', '--disable-background-networking', '--disable-component-update'])
            try:
                context = await _new_ass_context(browser, session)
                seen = []
                async def serve(route):
                    seen.append(route.request.url)
                    await route.fulfill(status=200, content_type='text/html', body='<!doctype html><title>synthetic</title>')
                await context.route('**/*', serve)
                page = await _new_task_page(context, session)
                assert seen == []  # seed由page本地route应答，不发网站探针。
                await page.goto('https://www.goofish.com/')
                values = await page.evaluate('''() => ({size: localStorage.getItem('fixture').length,
                    session: sessionStorage.getItem('fixture'), proto: sessionStorage.getItem('__proto__')})''')
                assert values == {'size': 118331, 'session': 'STORAGE_DUMMY', 'proto': 'not-code'}
                cookies = await context.cookies()
                assert {(c['domain'], c['name']) for c in cookies} == {('.goofish.com', 'sid'), ('www.goofish.com', 'sid')}
                await page.evaluate("sessionStorage.removeItem('fixture'); localStorage.removeItem('fixture')")
                await page.reload()
                assert await page.evaluate("[sessionStorage.getItem('fixture'), localStorage.getItem('fixture')]") == [None, None]
                await page.goto('https://outside.invalid/')
                assert await page.evaluate("[sessionStorage.getItem('fixture'), localStorage.getItem('fixture')]") == [None, None]
                await context.close()
            finally:
                await browser.close()
    asyncio.run(run())
