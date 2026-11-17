"""延迟页面脚本，验证过早点击不会触发原生表单跳转或把令牌放进URL。"""

import asyncio
import json
import threading

import httpx
from websockets.asyncio.client import connect

from fitlife import ui_check

gate = threading.Event()
hold = [False]
create_app = ui_check.create_app


def delayed_app(*args, **kwargs):
    app = create_app(*args, **kwargs)

    @app.middleware("http")
    async def delay_script(request, next_):
        if hold[0] and request.url.path == "/assets/layout.js":
            await asyncio.to_thread(gate.wait, 10)
        return await next_(request)

    return app


async def check(port, token, folder, report):
    pages = httpx.get(f"http://127.0.0.1:{port}/json/list", trust_env=False).json()
    page = next(p for p in pages if p["type"] == "page")
    async with connect(page["webSocketDebuggerUrl"], max_size=8_000_000) as ws:
        browser = ui_check.Browser(ws)
        await browser.call("Runtime.enable")
        await browser.wait("!!document.getElementById('token')")
        hold[0] = True
        await browser.call("Page.reload", {"ignoreCache": True})
        for _ in range(100):
            if await browser.evaluate(
                "!!document.getElementById('token') && "
                "document.documentElement.dataset.appReady!=='true'"
            ):
                break
            await asyncio.sleep(0.02)
        else:
            raise RuntimeError("STARTUP_DELAY_NOT_OBSERVED")
        assert await browser.evaluate(
            "document.querySelector('#login-form button').disabled && "
            "document.getElementById('login-form').method==='post'"
        )
        await browser.evaluate(
            f"document.getElementById('token').value={json.dumps(token)};"
            "document.querySelector('#login-form button').click()"
        )
        assert await browser.evaluate(
            "location.search==='' && document.getElementById('workspace').hidden"
        )
        gate.set()
        await browser.wait("!document.querySelector('#login-form button').disabled")
        await browser.evaluate("document.querySelector('#login-form button').click()")
        await browser.wait("!document.getElementById('workspace').hidden && !busy")
        assert await browser.evaluate("location.search===''")
        assert not browser.errors
        report["checks"] = [
            "early_login_blocked",
            "token_never_enters_url",
            "login_after_script_ready",
        ]
        report["phase"] = "completed"


if __name__ == "__main__":
    ui_check.create_app = delayed_app
    try:
        ui_check.main(checker=check)
    finally:
        gate.set()
        ui_check.create_app = create_app
