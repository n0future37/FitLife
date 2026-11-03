"""真实浏览器检验调整差异、依据变化失效和重新确认；固定模型与合成数据。"""

import base64
import json
from datetime import date
from uuid import uuid4

import httpx
from websockets.asyncio.client import connect

from fitlife.api_check import headers
from fitlife.business import today
from fitlife.plan_types import PlanRequest
from fitlife.planning import PlanningService
from fitlife.task_eval import DATA, FixedTaskModel, apply, resolved
from fitlife.ui_check import Browser
from fitlife.ui_check import main as ui_main

CASE = resolved(
    next(
        json.loads(line)
        for line in DATA.read_text(encoding="utf-8").splitlines()
        if json.loads(line)["id"] == "adjust-future-2"
    )
)


def setup(engine, actor):
    service = PlanningService(engine)
    apply(
        service,
        actor,
        service.propose(actor, "profile_patch", CASE["initial"]["profile"], str(uuid4())),
    )
    for week in CASE["initial"]["plans"]:
        apply(
            service,
            actor,
            service.propose_plan(
                actor,
                PlanRequest(week_start=date.fromisoformat(week), expected_plan_version=0),
                str(uuid4()),
            ),
        )


async def check(port, token, folder, report):
    pages = httpx.get(f"http://127.0.0.1:{port}/json/list", trust_env=False).json()
    page = next(p for p in pages if p["type"] == "page")
    async with connect(page["webSocketDebuggerUrl"], max_size=8000000) as socket:
        browser = Browser(socket)
        await browser.call("Runtime.enable")
        await browser.wait("!!document.getElementById('token')")
        await browser.evaluate(
            f"document.getElementById('token').value={json.dumps(token)};"
            "document.querySelector('#login-form button').click()"
        )
        await browser.wait(
            "!document.getElementById('workspace').hidden && "
            "!document.getElementById('send').disabled"
        )

        async def send():
            await browser.evaluate(
                f"document.getElementById('message').value={json.dumps(CASE['turns'][0]['message'])};"
                "document.getElementById('send').click()"
            )
            await browser.wait(
                "!!document.querySelector('#messages .candidate .actions button') && "
                "!document.getElementById('send').disabled"
            )

        async def confirm():
            await browser.evaluate(
                "[...document.querySelectorAll('#messages .candidate button')]"
                ".find(b=>b.textContent==='确认保存').click()"
            )

        await send()
        text = await browser.evaluate("document.getElementById('messages').textContent")
        assert "→" in text and "原计划版本 1" in text and "确认前会再次核对记录" in text
        assert "参考周暂无已保存计划，无法计算计划完成率" in text
        screenshot = await browser.call("Page.captureScreenshot", {"format": "png"})
        (folder / "adjustment-desktop.png").write_bytes(base64.b64decode(screenshot["data"]))
        origin = page["url"].rstrip("/")
        with httpx.Client(base_url=origin, trust_env=False, timeout=10) as client:
            action = client.post(
                "/activities/actions",
                headers=headers(token, uuid4()),
                json=dict(activity_id="walking", occurred_on=str(today()), minutes=5),
            ).json()
            client.post(
                f"/actions/{action['action_id']}/confirm",
                headers=headers(token),
                json=dict(revision=action["revision"], digest=action["digest"]),
            )
        await confirm()
        await browser.wait(
            "document.getElementById('messages').textContent.includes('已失效') && "
            "!document.getElementById('send').disabled"
        )
        await send()
        await browser.call(
            "Emulation.setDeviceMetricsOverride",
            dict(width=390, height=844, deviceScaleFactor=1, mobile=True),
        )
        assert await browser.evaluate("document.documentElement.scrollWidth<=window.innerWidth+1")
        screenshot = await browser.call("Page.captureScreenshot", {"format": "png"})
        (folder / "adjustment-mobile.png").write_bytes(base64.b64decode(screenshot["data"]))
        await confirm()
        await browser.wait(
            "document.getElementById('messages').textContent.includes('已保存') && "
            "!document.getElementById('send').disabled"
        )
        week = CASE["initial"]["plans"][0]
        plan = await browser.evaluate(f"api('/plans/{week}')")
        assert plan["version"] == 2 and len(plan["plan"]["sessions"]) == 2
        assert all(s["minutes"] == 15 for s in plan["plan"]["sessions"])
        assert not browser.errors
        report["checks"] = [
            "visible_before_after_difference",
            "visible_source_versions",
            "changed_activity_invalidates_confirmation",
            "regenerated_candidate_saved_once",
            "mobile_no_horizontal_overflow",
            "no_javascript_exception",
        ]


def main():
    ui_main(
        checker=check,
        setup=setup,
        app_options=dict(
            orchestration_mode="master",
            synthetic_evaluation=True,
            model_factory=lambda _: FixedTaskModel(CASE["turns"][0], "exercise"),
        ),
    )


if __name__ == "__main__":
    main()
