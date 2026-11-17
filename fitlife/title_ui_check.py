"""合成会话名称验收：首轮自动命名、用户修改、刷新及手机布局。"""

import base64
import json

import httpx
from websockets.asyncio.client import connect

from fitlife.model import Reply
from fitlife.session_check import DialogueFixture
from fitlife.ui_check import Browser, main

title_calls = []


class TitleFixture(DialogueFixture):
    supports_titles = True

    async def complete(self, instructions, items, tools):
        if instructions.startswith("conversation-title-v1"):
            title_calls.append(1)
            return Reply(text='{"title":"今日步行记录"}', response_model="title-fixture-v1")
        if items[-1].get("content") == "记录今天步行二十分钟":
            call = {
                "type": "function_call",
                "name": "propose_activity",
                "call_id": "title-activity",
                "arguments": json.dumps({"data": {"activity_id": "walking", "minutes": 20}}),
            }
            return Reply(calls=[call], continuation=[call], response_model="title-fixture-v1")
        return await super().complete(instructions, items, tools)


async def checker(port, token, folder, report):
    pages = httpx.get(f"http://127.0.0.1:{port}/json/list", trust_env=False).json()
    page = next(p for p in pages if p["type"] == "page")
    async with connect(page["webSocketDebuggerUrl"], max_size=8_000_000) as ws:
        browser = Browser(ws)
        await browser.call("Runtime.enable")

        async def login():
            await browser.wait(
                "document.getElementById('workspace').hidden && !!document.getElementById('token')"
            )
            await browser.evaluate(
                f"document.getElementById('token').value={json.dumps(token)};"
                "document.querySelector('#login-form button').click()"
            )
            await browser.wait("!document.getElementById('workspace').hidden && !busy")

        async def send():
            await browser.evaluate(
                "document.getElementById('message').value='记录今天步行二十分钟';"
                "document.getElementById('send').click()"
            )
            await browser.wait("!!document.querySelector('.candidate') && !busy")

        await login()
        report["phase"] = "first_turn"
        assert await browser.evaluate(
            "document.getElementById('conversations').selectedOptions[0].textContent==='新对话'"
        )
        await send()
        assert await browser.evaluate(
            "document.getElementById('conversations').selectedOptions[0].textContent==='今日步行记录'"
        )
        assert len(title_calls) == 1
        report["phase"] = "manual_rename"
        await browser.evaluate(
            "document.getElementById('rename-chat').click();"
            "document.getElementById('conversation-title').value='<b>我的步行</b>';"
            "document.querySelector('#conversation-title-form button').click()"
        )
        await browser.wait("document.getElementById('conversation-title-form').hidden && !busy")
        assert await browser.evaluate(
            "document.getElementById('conversations').selectedOptions[0].textContent"
            "==='<b>我的步行</b>' "
            "&& !document.querySelector('#conversations b')"
        )
        await send()
        assert len(title_calls) == 1
        report["phase"] = "reload"
        await browser.call("Page.reload", {"ignoreCache": True})
        await login()
        assert await browser.evaluate(
            "document.getElementById('conversations').selectedOptions[0].textContent==='<b>我的步行</b>'"
        )
        data = await browser.call("Page.captureScreenshot", {"format": "png"})
        (folder / "conversation-names-desktop.png").write_bytes(base64.b64decode(data["data"]))
        await browser.call(
            "Emulation.setDeviceMetricsOverride",
            {"width": 390, "height": 844, "deviceScaleFactor": 1, "mobile": True},
        )
        await browser.evaluate("document.getElementById('rename-chat').click()")
        assert await browser.evaluate("document.documentElement.scrollWidth<=innerWidth+1")
        data = await browser.call("Page.captureScreenshot", {"format": "png"})
        (folder / "conversation-names-mobile.png").write_bytes(base64.b64decode(data["data"]))
        assert not browser.errors
        report["checks"] = [
            "new_conversation_placeholder",
            "agent_name_after_first_success",
            "manual_name_saved",
            "title_rendered_as_inert_text",
            "later_turn_cannot_overwrite_user_name",
            "name_persists_after_reload",
            "mobile_rename_no_overflow",
            "no_javascript_exception",
        ]
        report["phase"] = "completed"


if __name__ == "__main__":
    main(checker=checker, app_options={"model_factory": lambda _: TitleFixture()})
