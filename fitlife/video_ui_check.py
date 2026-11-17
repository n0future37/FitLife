"""Real browser+native pose+MySQL check on a development video; zero cloud calls."""

import base64
import json
from pathlib import Path

import httpx
from websockets.asyncio.client import connect

from fitlife.ui_check import Browser, main

ROOT = Path(__file__).resolve().parents[1]


def forbidden_model(_):
    raise AssertionError("VIDEO_COUNT_MUST_NOT_INITIALIZE_CLOUD_MODEL")


async def check(port, token, folder, report):
    pages = httpx.get(f"http://127.0.0.1:{port}/json/list", trust_env=False).json()
    page = next(p for p in pages if p["type"] == "page")
    async with connect(page["webSocketDebuggerUrl"], max_size=8_000_000) as socket:
        browser = Browser(socket)
        await browser.call("Runtime.enable")
        await browser.wait("!!document.getElementById('media-form')")
        await browser.evaluate(
            f"document.getElementById('token').value={json.dumps(token)};"
            "document.querySelector('#login-form button').click()"
        )
        await browser.wait("!document.getElementById('workspace').hidden && !busy")
        await browser.evaluate(
            "document.querySelector('[data-tab=media]').click();documen"
            "t.getElementById('media-task').value='workout_video';docum"
            "ent.getElementById('media-task').dispatchEvent(new Event('"
            "change'))"
        )
        assert await browser.evaluate("!document.getElementById('media-exercise-label').hidden")
        await browser.evaluate("document.getElementById('media-exercise').value='push_up'")
        doc = await browser.call("DOM.getDocument")
        node = await browser.call(
            "DOM.querySelector", {"nodeId": doc["root"]["nodeId"], "selector": "#media-file"}
        )
        await browser.call(
            "DOM.setFileInputFiles",
            {
                "nodeId": node["nodeId"],
                "files": [str(ROOT / "data/media-eval-v2/repcount/stu9_53.mp4")],
            },
        )
        report["phase"] = "real_upload_and_native_count"
        await browser.evaluate("document.querySelector('#media-form button').click()")
        await browser.wait("!!document.querySelector('#media-result form') && !busy")
        automatic = await browser.evaluate("mediaJob.result.counting.repetitions")
        assert isinstance(automatic, int) and automatic > 0
        manual = automatic + 1
        await browser.evaluate(
            f"globalThis.checkAutomatic={automatic};globalThis.checkManual={manual}"
        )
        assert await browser.evaluate(
            "mediaJob.model_calls===0 && mediaJob.result.counting.full_"
            "timeline_processed && mediaJob.result.counting.repetitions"
            "===checkAutomatic"
        )
        report["automatic_count"] = automatic
        report["confirmed_count"] = manual
        report["mode"] = "mechanism-only, simulated user correction, not accuracy grading"
        report["job_id"] = await browser.evaluate("mediaJob.job_id")
        # Correct before review; review must not silently restore the automatic value.
        await browser.evaluate(
            "[...document.querySelectorAll('#media-result label')].find"
            "(e=>e.textContent.includes('本人核对的总次数')).querySelector('inp"
            "ut').value=String(checkManual);document.querySelector('#media-result form"
            " button[type=submit]').click()"
        )
        await browser.wait("mediaJob.reviewed && !busy")
        assert await browser.evaluate(
            "[...document.querySelectorAll('#media-result label')].find"
            "(e=>e.textContent.includes('本人核对的总次数')).querySelector('inp"
            "ut').value===String(checkManual)"
        )
        await browser.evaluate(
            "[...document.querySelectorAll('#media-result button')].fin"
            "d(b=>b.textContent==='生成待确认记录').click()"
        )
        await browser.wait(
            "!!document.querySelector('#media-candidate .candidate .actions button') && !busy"
        )
        assert await browser.evaluate(
            "document.getElementById('media-candidate').textContent.includes(String(checkManual))"
        )
        report["phase"] = "candidate_and_confirmation"
        assert await browser.evaluate(
            "(async()=>((await api(`/domain-records/workout?start_date="
            "${today}&end_date=${today}`)).records.length===0))()"
        )
        await browser.evaluate(
            "document.querySelector('#media-candidate .candidate .actions button').click()"
        )
        await browser.wait(
            "!document.querySelector('#media-candidate .candidate .actions button') && !busy"
        )
        assert await browser.evaluate(
            "(async()=>((await api(`/domain-records/workout?start_date="
            "${today}&end_date=${today}`)).records["
            "0].values.items[0].reps===String(checkManual)))()"
        )
        assert await browser.evaluate("mediaJob.result.counting.repetitions===checkAutomatic")
        old_origin = await browser.evaluate("performance.timeOrigin")
        await browser.call("Page.reload", {"ignoreCache": True})
        await browser.wait(
            f"performance.timeOrigin!=={old_origin} && !!document.getElementById('media-form')"
        )
        await browser.evaluate(
            f"document.getElementById('token').value={json.dumps(token)};"
            "document.querySelector('#login-form button').click()"
        )
        await browser.wait("!document.getElementById('workspace').hidden && !busy")
        await browser.evaluate(
            "document.querySelector('[data-tab=media]').click();documen"
            "t.querySelector('#media-jobs button').click()"
        )
        await browser.wait("!!document.querySelector('#media-result form') && !busy")
        await browser.evaluate(
            f"globalThis.checkAutomatic={automatic};globalThis.checkManual={manual}"
        )
        assert await browser.evaluate(
            "(async()=>(mediaJob.result.counting.repetitions===checkAutomatic && (a"
            "wait api(`/domain-records/workout?start_date=${today}&end_"
            "date=${today}`)).records[0].values.ite"
            "ms[0].reps===String(checkManual)))()"
        )
        screenshot = await browser.call("Page.captureScreenshot", {"format": "png"})
        (folder / "video-desktop.png").write_bytes(base64.b64decode(screenshot["data"]))
        await browser.call(
            "Emulation.setDeviceMetricsOverride",
            {"width": 390, "height": 844, "deviceScaleFactor": 1, "mobile": True},
        )
        assert await browser.evaluate("document.documentElement.scrollWidth<=window.innerWidth")
        screenshot = await browser.call("Page.captureScreenshot", {"format": "png"})
        (folder / "video-mobile.png").write_bytes(base64.b64decode(screenshot["data"]))
        await browser.evaluate("document.getElementById('logout').click()")
        await browser.wait("document.getElementById('workspace').hidden && !busy")
        assert await browser.evaluate(
            "!document.getElementById('media-result').children.length &"
            "& !document.getElementById('media-preview').children.lengt"
            "h"
        )
        assert not browser.errors
        report["phase"] = "completed"
        report["checks"] = [
            "selected_action",
            "private_real_video_upload",
            "complete_timeline_native_count",
            "zero_cloud_calls",
            "correction_survives_review",
            "no_record_before_confirmation",
            "confirmed_corrected_value",
            "original_automatic_evidence_immutable",
            "reload_keeps_job_and_record",
            "mobile_no_overflow",
            "logout_clears_private_media",
            "no_javascript_exception",
        ]


if __name__ == "__main__":
    main(
        checker=check,
        app_options={"media_worker_enabled": True, "media_model_factory": forbidden_model},
    )
