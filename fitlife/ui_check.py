"""本机Chrome无头界面验收。独立临时浏览器、合成用户、不读取个人浏览器资料。"""

import asyncio
import base64
import json
import os
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from uuid import uuid4

import httpx
import uvicorn
from websockets.asyncio.client import connect

from fitlife.api import create_app
from fitlife.auth import issue_token
from fitlife.database import (
    DatabaseSettings,
)
from fitlife.manage import migrate
from fitlife.model import Reply
from fitlife.session_check import DialogueFixture

ROOT = Path(__file__).resolve().parents[1]


class Browser:
    def __init__(self, websocket):
        self.websocket, self.sequence, self.errors = websocket, 0, []

    async def call(self, method, params=None):
        self.sequence += 1
        sequence = self.sequence
        await self.websocket.send(
            json.dumps({"id": sequence, "method": method, "params": params or {}})
        )
        while True:
            response = json.loads(await asyncio.wait_for(self.websocket.recv(), 20))
            if response.get("method") == "Runtime.exceptionThrown":
                self.errors.append("JAVASCRIPT_EXCEPTION")
            if response.get("id") == sequence:
                if "error" in response:
                    raise RuntimeError("BROWSER_COMMAND_FAILED")
                return response["result"]

    async def evaluate(self, expression):
        result = await self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
        )
        if result.get("exceptionDetails"):
            raise RuntimeError("BROWSER_EVALUATION_FAILED")
        return result.get("result", {}).get("value")

    async def wait(self, expression):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                if await self.evaluate(expression):
                    return
            except RuntimeError as exc:
                if str(exc) not in {"BROWSER_COMMAND_FAILED", "BROWSER_EVALUATION_FAILED"}:
                    raise
            await asyncio.sleep(0.1)
        raise RuntimeError("BROWSER_CONDITION_TIMEOUT")


async def check_browser(port, token, folder, report):
    pages = httpx.get(f"http://127.0.0.1:{port}/json/list", trust_env=False).json()
    page = next(p for p in pages if p["type"] == "page")
    async with connect(page["webSocketDebuggerUrl"], max_size=8_000_000) as websocket:
        browser = Browser(websocket)
        report["phase"] = "login"
        await browser.call("Runtime.enable")
        await browser.wait("!!document.getElementById('login-form')")
        await browser.evaluate(
            f"document.getElementById('token').value={json.dumps(token)};"
            "document.querySelector('#login-form button').click();"
        )
        await browser.wait(
            "!document.getElementById('workspace').hidden && "
            "!document.getElementById('send').disabled"
        )
        report["phase"] = "clarification"
        await browser.evaluate(
            "document.getElementById('message').value='记录今天步行';document.getElementById('send').click()"
        )
        await browser.wait(
            "document.getElementById('messages').textContent.includes('多少分钟')"
            " && !document.getElementById('send').disabled"
        )
        await browser.evaluate(
            "document.getElementById('message').value='二十分钟';document.getElementById('send').click()"
        )
        report["phase"] = "candidate"
        await browser.wait(
            "!!document.querySelector('#messages .candidate .actions button')"
            " && !document.getElementById('send').disabled"
        )
        assert await browser.evaluate(
            "document.getElementById('messages').textContent.includes('20 分钟')"
        )
        # 在浏览器中验证文本节点渲染：恶意HTML不能成为可执行DOM。
        assert await browser.evaluate(
            "(()=>{const p=document.createElement('div');candidate({kind:'profile_patch',"
            "status:'applied',payload:{goal:'<img src=x onerror=alert(1)>'}},p);"
            "return !p.querySelector('img') && p.textContent.includes('<img');})()"
        )
        capture = await browser.call("Page.captureScreenshot", {"format": "png"})
        (folder / "chat-desktop.png").write_bytes(base64.b64decode(capture["data"]))
        await browser.evaluate(
            "document.querySelector('#messages .candidate .actions button').click()"
        )
        await browser.wait(
            "document.querySelector('#messages .candidate .badge').textContent==='已保存'"
            " && !document.getElementById('send').disabled"
        )
        await browser.evaluate("document.querySelector('[data-tab=activity]').click()")
        await browser.wait("document.getElementById('records').textContent.includes('共记录 1 次')")
        await browser.evaluate(
            "document.querySelector('[data-tab=chat]').click();"
            "document.getElementById('message').value='恢复验收';"
            "document.getElementById('send').click()"
        )
        report["phase"] = "persisted_retry"
        await browser.wait(
            "!!document.querySelector('.recover-request') && "
            "!document.getElementById('send').disabled"
        )
        failed_key = await browser.evaluate(
            "document.querySelector('.recover-request').dataset.requestKey"
        )
        await browser.call("Page.reload")
        await browser.wait(
            "document.readyState==='complete' && "
            "document.getElementById('workspace')?.hidden===true && "
            "document.getElementById('token')?.value===''"
        )
        await browser.evaluate(
            f"document.getElementById('token').value={json.dumps(token)};"
            "document.querySelector('#login-form button').click()"
        )
        await browser.wait(
            "!document.getElementById('workspace').hidden && "
            "!!document.querySelector('.recover-request') && "
            "!document.getElementById('send').disabled"
        )
        assert (
            await browser.evaluate("document.querySelector('.recover-request').dataset.requestKey")
            == failed_key
        )
        await browser.evaluate("document.querySelector('.recover-request').click()")
        await browser.wait(
            "document.getElementById('messages').textContent.includes('本次请求已恢复')"
            " && !document.querySelector('.recover-request') && "
            "!document.getElementById('send').disabled"
        )
        await browser.evaluate("document.querySelector('[data-tab=activity]').click()")
        assert await browser.evaluate(
            "document.getElementById('records').textContent.includes('共记录 1 次')"
        )
        await browser.call(
            "Emulation.setDeviceMetricsOverride",
            {"width": 390, "height": 844, "deviceScaleFactor": 1, "mobile": True},
        )
        assert await browser.evaluate("document.documentElement.scrollWidth <= innerWidth")
        capture = await browser.call("Page.captureScreenshot", {"format": "png"})
        (folder / "activity-mobile.png").write_bytes(base64.b64decode(capture["data"]))
        assert await browser.evaluate("document.getElementById('status').textContent === ''")
        report["phase"] = "expired_token_logout"
        await browser.evaluate("api('/auth/token','DELETE')")
        await browser.evaluate("document.getElementById('logout').click()")
        await browser.wait(
            "document.getElementById('workspace').hidden && "
            "!document.getElementById('login').hidden"
        )
        assert await browser.evaluate(
            "token==='' && document.getElementById('messages').textContent===''"
            " && document.getElementById('records').textContent===''"
        )
        assert not browser.errors
        report["phase"] = "completed"
        report["checks"] = [
            "token_login",
            "multi_turn_candidate",
            "confirm_then_activity_visible",
            "safe_text_rendering",
            "mobile_no_horizontal_overflow",
            "no_javascript_exception",
            "persisted_failed_request_retry_after_browser_reload",
            "expired_token_logout_clears_local_private_state",
        ]


def main(checker=check_browser, app_options=None, setup=None):
    settings = DatabaseSettings(environment="test")
    engine = settings.engine()
    migrate(engine, "test")
    actor, token = issue_token(engine)
    folder = ROOT / "artifacts/ui-checks" / str(uuid4())
    folder.mkdir(parents=True)
    report = {"status": "failed", "checks": []}
    browser_process = server = thread = None
    try:
        if setup:
            setup(engine, actor)
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        recovery_attempts = [0]

        class UIModel(DialogueFixture):
            async def complete(self, instructions, items, tools):
                if items[-1].get("content") == "恢复验收":
                    recovery_attempts[0] += 1
                    if recovery_attempts[0] == 1:
                        raise PermissionError("INJECTED_UI_MODEL_FAILURE")
                    return Reply(text="本次请求已恢复。", response_model="ui-recovery-fixture-v1")
                return await super().complete(instructions, items, tools)

        options = dict(
            orchestration_mode="single", engine=engine, model_factory=lambda _: UIModel()
        )
        options.update(app_options or {})
        app = create_app(settings, **options)
        server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
        thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        if not server.started:
            raise RuntimeError("SERVER_START_TIMEOUT")
        chrome = Path(
            os.environ.get(
                "FITLIFE_TEST_CHROME", r"C:\Program Files\Google\Chrome\Application\chrome.exe"
            )
        )
        if not chrome.is_file():
            raise RuntimeError("CHROME_REQUIRED")
        with tempfile.TemporaryDirectory(prefix="fitlife-ui-") as profile:
            browser_process = subprocess.Popen(
                [
                    str(chrome),
                    "--headless=new",
                    "--disable-gpu",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--remote-debugging-port=0",
                    "--window-size=1280,1000",
                    "--user-data-dir=" + profile,
                    f"http://127.0.0.1:{sock.getsockname()[1]}/",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            try:
                port_file = Path(profile) / "DevToolsActivePort"
                deadline = time.monotonic() + 15
                report["phase"] = "devtools_port_ready"
                while time.monotonic() < deadline:
                    try:
                        port = int(port_file.read_text().splitlines()[0])
                        break
                    except (FileNotFoundError, PermissionError, ValueError, IndexError):
                        time.sleep(0.1)
                else:
                    raise RuntimeError("BROWSER_DEVTOOLS_PORT_TIMEOUT")
                asyncio.run(checker(port, token, folder, report))
                report["status"] = "passed"
            finally:
                browser_process.terminate()
                browser_process.wait(timeout=10)
    except Exception as exc:
        report["error_type"] = type(exc).__name__
        if isinstance(exc, RuntimeError) and str(exc).startswith(
            ("BROWSER_", "CHROME_", "SERVER_")
        ):
            report["error_code"] = str(exc)
    finally:
        if server:
            server.should_exit = True
        if thread:
            thread.join(timeout=15)
        from fitlife.evaluate import clean

        clean(engine, [actor])
        engine.dispose()
        report["fixture_cleanup"] = "verified"
        (folder / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(f"界面验收：{report['status']}；报告：{folder / 'report.json'}")
    if report["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
