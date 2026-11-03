"""真实浏览器/上传/数据库/后台任务，视觉模型固定响应；不调用付费模型。"""

import base64
import json
import struct
import zlib

import httpx
from websockets.asyncio.client import connect

from fitlife.model import Reply
from fitlife.ui_check import Browser, main


def fixture_png():
    def chunk(tag, data):
        return (
            struct.pack("!I", len(data))
            + tag
            + data
            + struct.pack("!I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack("!2I5B", 64, 64, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress((b"\x00" + b"\x00\xff\x00" * 64) * 64))
        + chunk(b"IEND", b"")
    )


class Vision:
    async def complete(self, *args):
        return Reply(
            text=json.dumps(
                {
                    "kind": "nutrition_label",
                    "name": "合成食品",
                    "basis_amount": 100,
                    "unit": "g",
                    "energy_value": 100,
                    "energy_unit": "kcal",
                    "protein_g": 5,
                    "carbs_g": 10,
                    "fat_g": 2,
                    "evidence": "synthetic fixture",
                    "uncertainty": "合成界面验收，不是真实识别准确率",
                }
            ),
            response_model="ui-vision-fixture",
        )

    async def close(self):
        pass


async def check(port, token, folder, report):
    pages = httpx.get(f"http://127.0.0.1:{port}/json/list", trust_env=False).json()
    page = next(p for p in pages if p["type"] == "page")
    async with connect(page["webSocketDebuggerUrl"], max_size=8000000) as socket:
        browser = Browser(socket)
        await browser.call("Runtime.enable")
        await browser.wait(
            "!!document.getElementById('media-form') && typeof loadMediaJobs==='function'"
        )
        await browser.evaluate(
            f"document.getElementById('token').value={json.dumps(token)};"
            "document.querySelector('#login-form button').click()"
        )
        await browser.wait("!document.getElementById('workspace').hidden && !busy")
        await browser.evaluate("document.querySelector('[data-tab=media]').click()")
        path = folder / "synthetic.png"
        path.write_bytes(fixture_png())
        doc = await browser.call("DOM.getDocument")
        node = await browser.call(
            "DOM.querySelector", {"nodeId": doc["root"]["nodeId"], "selector": "#media-file"}
        )
        await browser.call(
            "DOM.setFileInputFiles", {"nodeId": node["nodeId"], "files": [str(path)]}
        )
        report["phase"] = "upload_and_analyze"
        await browser.evaluate("document.querySelector('#media-form button').click()")
        await browser.wait("!!document.querySelector('#media-result form') && !busy")
        await browser.evaluate(
            "document.querySelector('#media-result form button[type=submit]').click()"
        )
        await browser.wait(
            (
                "[...document.querySelect"
                "orAll('#media-result but"
                "ton')].some(b=>b.textCon"
                "tent==='生成待确认记录') && !bu"
                "sy"
            )
        )
        await browser.evaluate(
            (
                "[...document.querySelect"
                "orAll('#media-result but"
                "ton')].find(b=>b.textCon"
                "tent==='生成待确认记录').click("
                ")"
            )
        )
        await browser.wait(
            "!!document.querySelector('#media-candidate .candidate .actions button') && !busy"
        )
        assert await browser.evaluate(
            "document.getElementById('media-candidate').textContent.includes('合成食品')"
        )
        assert not await browser.evaluate(
            ("document.getElementById('media-candidate').textContent.includes('[object Object]')")
        )
        screenshot = await browser.call("Page.captureScreenshot", {"format": "png"})
        (folder / "media-desktop.png").write_bytes(base64.b64decode(screenshot["data"]))
        report["phase"] = "confirm"
        await browser.evaluate(
            ("document.querySelector('#media-candidate .actions button').click()")
        )
        await browser.wait(
            "document.querySelector('#media-candidate .badge').textContent==='已保存' && !busy"
        )
        report["phase"] = "reload"
        old_origin = await browser.evaluate("performance.timeOrigin")
        await browser.call("Page.reload")
        await browser.wait(
            f"performance.timeOrigin!=={old_origin} && "
            "!!document.getElementById('login-form') && "
            "!document.getElementById('login').hidden && "
            "typeof loadMediaJobs==='function'"
        )
        await browser.evaluate(
            f"document.getElementById('token').value={json.dumps(token)};"
            "document.querySelector('#login-form button').click()"
        )
        await browser.wait("!document.getElementById('workspace').hidden && !busy")
        await browser.evaluate(
            (
                "document.querySelector('"
                "[data-tab=media]').click"
                "();document.querySelecto"
                "r('#media-jobs button')."
                "click()"
            )
        )
        await browser.wait(
            "document.getElementById('media-result').textContent"
            ".includes('生成待确认记录') && !busy"
        )
        await browser.call(
            "Emulation.setDeviceMetricsOverride",
            {"width": 390, "height": 844, "deviceScaleFactor": 1, "mobile": True},
        )
        assert await browser.evaluate(("document.documentElement.scrollWidth<=window.innerWidth"))
        screenshot = await browser.call("Page.captureScreenshot", {"format": "png"})
        (folder / "media-mobile.png").write_bytes(base64.b64decode(screenshot["data"]))
        await browser.evaluate("document.getElementById('logout').click()")
        await browser.wait("document.getElementById('workspace').hidden && !busy")
        assert await browser.evaluate(
            (
                "!document.getElementById"
                "('media-preview').childr"
                "en.length && !document.g"
                "etElementById('media-res"
                "ult').children.length &&"
                " !document.getElementByI"
                "d('media-candidate').chi"
                "ldren.length"
            )
        )
        assert not browser.errors
        report["checks"] = [
            "private_upload_in_browser",
            "background_fixed_vision_and_result_review",
            "human_readable_candidate_and_explicit_confirmation",
            "persisted_job_after_reload",
            "mobile_no_horizontal_overflow",
            "logout_clears_media_and_no_javascript_exception",
        ]


if __name__ == "__main__":
    main(
        checker=check,
        app_options={"media_worker_enabled": True, "media_model_factory": lambda _: Vision()},
    )
