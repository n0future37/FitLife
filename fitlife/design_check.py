"""新版页面桌面/手机检查；独立浏览器和合成历史，不访问旧项目。"""

import base64
import hashlib
import json
import tempfile
from pathlib import Path

import httpx
from websockets.asyncio.client import connect

from fitlife.legacy_archive import canonical, import_history
from fitlife.ui_check import Browser, main


def setup(engine, actor):
    rows = [
        {
            "table": "foods",
            "key": "1",
            "payload": {"id": 1, "name": "合成示例食品", "source": "待核对标签"},
        },
        {"table": "conversations", "key": "1", "payload": {"id": 1, "title": "合成历史对话"}},
        {
            "table": "messages",
            "key": "1",
            "payload": {
                "id": 1,
                "conversation_id": 1,
                "role": "user",
                "content": "<img src=x onerror=window.__history_xss=1>",
            },
        },
    ]
    rows += [
        {
            "table": "meals",
            "key": str(i),
            "payload": {"id": i, "meal_date": "2020-01-01", "meal_type": "合成餐食"},
        }
        for i in range(1, 32)
    ]
    for row in rows:
        row["review_status"] = "historical_unverified"
    raw = b"".join(canonical(row) + b"\n" for row in rows)
    sha = hashlib.sha256(raw).hexdigest()
    with tempfile.TemporaryDirectory(prefix="fitlife-design-history-") as folder:
        folder = Path(folder)
        (folder / "records.jsonl").write_bytes(raw)
        (folder / "manifest.json").write_bytes(
            canonical(
                {
                    "format": "fitlife-history-v1",
                    "dataset_id": sha,
                    "files": {"records.jsonl": sha},
                    "record_count": len(rows),
                    "table_counts": {"foods": 1, "conversations": 1, "messages": 1, "meals": 31},
                }
            )
        )
        import_history(engine, folder, actor.user_id)


async def checker(port, token, folder, report):
    pages = httpx.get(f"http://127.0.0.1:{port}/json/list", trust_env=False).json()
    page = next(p for p in pages if p["type"] == "page")
    async with connect(page["webSocketDebuggerUrl"], max_size=8_000_000) as ws:
        browser = Browser(ws)
        await browser.call("Runtime.enable")
        await browser.wait("!!document.getElementById('login-form')")

        async def capture(name):
            data = await browser.call("Page.captureScreenshot", {"format": "png"})
            (folder / name).write_bytes(base64.b64decode(data["data"]))

        async def viewport(width, height):
            await browser.call(
                "Emulation.setDeviceMetricsOverride",
                {
                    "width": width,
                    "height": height,
                    "deviceScaleFactor": 1,
                    "mobile": width < 600,
                },
            )

        await viewport(1440, 1050)
        await capture("login-desktop.png")
        await browser.call(
            "Input.dispatchKeyEvent",
            {"type": "keyDown", "key": "Tab", "code": "Tab", "windowsVirtualKeyCode": 9},
        )
        assert await browser.evaluate("document.activeElement.classList.contains('skip-link')")
        assert await browser.evaluate(
            "getComputedStyle(document.activeElement).outlineStyle!=='none'"
        )
        report["checks"].append("keyboard_skip_link_visible_focus")
        await viewport(390, 844)
        assert await browser.evaluate("document.documentElement.scrollWidth<=innerWidth+1")
        await browser.call(
            "Emulation.setEmulatedMedia",
            {"features": [{"name": "prefers-reduced-motion", "value": "reduce"}]},
        )
        assert await browser.evaluate(
            "getComputedStyle(document.querySelector('button')).transitionDuration==='0s'"
        )
        await capture("login-mobile.png")
        report["checks"].append("login_mobile_no_overflow_reduced_motion")
        await browser.evaluate(
            f"document.getElementById('token').value={json.dumps(token)};"
            "document.querySelector('#login-form button').click()"
        )
        await browser.wait(
            "!document.getElementById('workspace').hidden && !document.ge"
            "tElementById('send').disabled"
        )
        await viewport(1440, 1050)
        await browser.evaluate("document.activeElement.blur()")
        await capture("chat-desktop.png")
        await browser.evaluate("document.querySelector('[data-prompt]').click()")
        assert await browser.evaluate(
            "document.getElementById('message').value==='记录今天步行二十分钟'"
        )
        assert await browser.evaluate("!document.querySelector('#messages .bubble')")
        report["checks"].append("suggestion_fills_without_model_call")
        for name in ("plan", "activity", "profile", "media", "domain", "chat"):
            await browser.evaluate(f"document.querySelector('[data-tab={name}]').click()")
            assert await browser.evaluate(
                f"!document.getElementById('{name}').hidden && location.hash==='#{name}'"
            )
        report["checks"].append("all_existing_tabs_and_url_state")
        await browser.evaluate("document.querySelector('[data-tab=history]').click()")
        await browser.wait(
            "document.querySelectorAll('#history-output .history-row').le"
            "ngth===1 && !document.getElementById('send').disabled"
        )
        await capture("history-desktop.png")
        await browser.evaluate(
            "document.getElementById('history-table').value='meals';docum"
            "ent.querySelector('#history-form button').click()"
        )
        await browser.wait(
            "document.querySelectorAll('#history-output .history-row').le"
            "ngth===30 && !document.getElementById('send').disabled"
        )
        await browser.evaluate("document.querySelector('.history-pagination button').click()")
        await browser.wait(
            "document.querySelectorAll('#history-output .history-row').le"
            "ngth===1 && document.querySelector('.history-pagination').te"
            "xtContent.includes('第 2 页') && !document.getElementById('sen"
            "d').disabled"
        )
        report["checks"].append("history_actual_pagination_31_rows")
        await browser.evaluate(
            "document.getElementById('history-table').value='messages';do"
            "cument.querySelector('#history-form button').click()"
        )
        await browser.wait(
            "!!document.querySelector('#history-output pre') && !document"
            ".getElementById('send').disabled"
        )
        assert await browser.evaluate(
            "!document.querySelector('#history-output img') && document.q"
            "uerySelector('#history-output pre').textContent.includes('<i"
            "mg') && !window.__history_xss"
        )
        report["checks"].append("historical_html_is_inert_text")
        await viewport(390, 844)
        for name in ("chat", "plan", "activity", "profile", "media", "domain", "history"):
            await browser.evaluate(f"document.querySelector('[data-tab={name}]').click()")
            await browser.wait("!document.getElementById('send').disabled")
            assert await browser.evaluate("document.documentElement.scrollWidth<=innerWidth+1"), (
                name
            )
        await capture("history-mobile.png")
        report["checks"].append("all_seven_tabs_mobile_no_document_overflow")
        # A delayed response must not restore private history after logout.
        await browser.evaluate(
            "window.__oldFetch=window.fetch;window.fetch=(...a)=>String(a"
            "[0]).endsWith('/history')?new Promise(r=>window.__releaseHis"
            "tory=r):window.__oldFetch(...a);void loadHistory();"
        )
        await browser.wait("!!window.__releaseHistory")
        await browser.evaluate("document.getElementById('logout').click()")
        await browser.wait("document.getElementById('workspace').hidden")
        await browser.evaluate(
            "window.__releaseHistory(new Response(JSON.stringify({record_"
            "count:1,tables:[{table:'foods',label:'SHOULD_NOT_RENDER',cou"
            "nt:1}]}),{status:200,headers:{'Content-Type':'application/js"
            "on'}}));window.fetch=window.__oldFetch;"
        )
        await browser.wait(
            "document.getElementById('history-output').textContent==='' &"
            "& document.getElementById('history-counts').textContent===''"
        )
        assert not await browser.evaluate("document.body.textContent.includes('SHOULD_NOT_RENDER')")
        report["checks"].append("late_history_response_cannot_restore_logged_out_data")
        assert not browser.errors
        report["checks"].append("no_javascript_exception")
        report["phase"] = "completed"


if __name__ == "__main__":
    main(checker=checker, setup=setup)
