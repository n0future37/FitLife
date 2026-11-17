"""小额硅基流动验收：持久化人民币预留、真实模型/思考参数/图片及只读API。"""

import argparse
import asyncio
import base64
import hashlib
import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
from fastapi.testclient import TestClient

from fitlife.api import create_app
from fitlife.auth import issue_token
from fitlife.business import today
from fitlife.database import DatabaseSettings
from fitlife.domains import DomainService
from fitlife.evaluate import clean
from fitlife.execution import Ledger, complete
from fitlife.manage import migrate
from fitlife.model import Settings, create_model
from fitlife.model_profiles import configured, resolve
from fitlife.tools import get_activity_catalog

ROOT = Path(__file__).resolve().parents[1]
BASE_URL = "https://api.siliconflow.cn/v1"
SOURCE = "https://www.siliconflow.cn/models"
IDS = {"Qwen/Qwen3-8B", "Qwen/Qwen3-32B", "Qwen/Qwen3-VL-8B-Instruct"}


def synthetic_card(path):
    """用本机标准字体生成合成测试卡，不读取个人素材或安装依赖。"""
    script = """
Add-Type -AssemblyName System.Drawing
$bitmap = New-Object System.Drawing.Bitmap 640,240
$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
$font = New-Object System.Drawing.Font 'Arial',44
try {
    $graphics.Clear([System.Drawing.Color]::White)
    $graphics.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::AntiAliasGridFit
    $graphics.DrawString('WALK 20 MIN',$font,[System.Drawing.Brushes]::Black,24,75)
    $bitmap.Save($env:FITLIFE_FIXTURE_PNG,[System.Drawing.Imaging.ImageFormat]::Png)
} finally { $font.Dispose(); $graphics.Dispose(); $bitmap.Dispose() }
"""
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        env={**os.environ, "FITLIFE_FIXTURE_PNG": str(path.resolve())},
        capture_output=True,
        check=True,
        timeout=15,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def public_prices(ids=None):
    ids = IDS if ids is None else set(ids)
    response = httpx.get(SOURCE, timeout=30)
    response.raise_for_status()
    found = {}
    for match in re.finditer(r"self\.__next_f\.push\((\[.*?\])\)</script>", response.text, re.S):
        try:
            payload = json.loads(match.group(1))[1]
        except (ValueError, TypeError, IndexError):
            continue
        if not isinstance(payload, str):
            continue
        for start in re.finditer(r'\{"modelId":', payload):
            try:
                model, _ = json.JSONDecoder().raw_decode(payload[start.start() :])
            except ValueError:
                continue
            if model.get("modelName") not in ids:
                continue
            assert model["currency"] == "¥" and model["priceUnit"] == "/ M Tokens"
            rates = {p["specification"]: p["price"] for p in model["pricing"]}
            assert {"prompt", "completion"} <= rates.keys()
            assert all(p["unitOfGood"] == "/ M Tokens" for p in model["pricing"])
            found[model["modelName"]] = {
                "rates_cny_per_million": rates,
                "context_length": model["contextLen"],
                "tools": model["functionCallSupport"],
                "images": model["vlm"],
            }
    assert found.keys() == ids, "OFFICIAL_PRICE_SNAPSHOT_INCOMPLETE"
    return found


class CNYBudget:
    def __init__(self, path, prices):
        self.path, self.prices, self.lock = path, prices, threading.Lock()
        self.state = (
            json.loads(path.read_text(encoding="utf-8"))
            if path.exists()
            else {
                "policy": "siliconflow-check-cny-v1",
                "limit_cny": "0.5",
                "attempts": [],
                "price_source": SOURCE,
                "actual_billed_cny": None,
            }
        )
        assert self.state["limit_cny"] == "0.5", "BUDGET_LIMIT_MISMATCH"

    def save(self):
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.path)

    def reserve(self, settings):
        if settings.base_url.rstrip("/") != BASE_URL or settings.model not in self.prices:
            raise ValueError("UNPRICED_SILICONFLOW_MODEL")
        price = self.prices[settings.model]
        rates = price["rates_cny_per_million"]
        output = settings.max_output_tokens + (settings.thinking_budget or 0)
        # Reserve the full model context, including image tokens; unknown usage keeps this hold.
        hold = (
            (
                Decimal(price["context_length"]) * Decimal(rates["prompt"])
                + output * Decimal(rates["completion"])
            )
            / 1000000
            * Decimal("1.25")
        )
        with self.lock:
            used = sum(Decimal(e["held_cny"]) for e in self.state["attempts"])
            if used + hold > Decimal(self.state["limit_cny"]) or len(self.state["attempts"]) >= 12:
                raise ValueError("SILICONFLOW_CHECK_BUDGET_EXHAUSTED")
            self.state["attempts"].append(
                {
                    "model": settings.model,
                    "held_cny": str(hold),
                    "reserved_cny": str(hold),
                    "rates_cny_per_million": rates,
                    "usage": None,
                    "enable_thinking": settings.enable_thinking,
                    "thinking_budget": settings.thinking_budget,
                }
            )
            self.save()
            return len(self.state["attempts"]) - 1

    def settle(self, index, reply):
        with self.lock:
            event = self.state["attempts"][index]
            event.update(
                usage=reply.usage,
                response_model=reply.response_model,
                provider_request_id=reply.request_id,
            )
            usage = reply.usage or {}
            if all(
                type(usage.get(k)) is int and usage[k] >= 0
                for k in ("input_tokens", "output_tokens")
            ):
                rates = event["rates_cny_per_million"]
                cost = (
                    usage["input_tokens"] * Decimal(rates["prompt"])
                    + usage["output_tokens"] * Decimal(rates["completion"])
                ) / 1000000
                event.update(estimated_cny=str(cost), held_cny=str(cost * Decimal("1.25")))
            self.save()

    def factory(self, settings):
        model, budget = create_model(settings), self

        class Budgeted:
            network_model = True

            def __init__(self):
                self.settings = settings

            async def complete(self, instructions, items, tools):
                index = budget.reserve(settings)
                reply = await model.complete(instructions, items, tools)
                budget.settle(index, reply)
                return reply

            async def close(self):
                await model.close()

        return Budgeted()


async def adapter_checks(settings, budget, folder, report, resume=None):
    tools = [
        {
            "type": "function",
            "name": "get_activity_catalog",
            "description": "查询应用支持的运动项目，只读。",
            "parameters": {
                "type": "object",
                "properties": {"category": {"type": "string", "enum": ["all"]}},
                "required": ["category"],
                "additionalProperties": False,
            },
        }
    ]
    ledger, trace = Ledger(), []
    report["adapter_runs"], report["trace"] = [], trace
    if resume:
        report["adapter_runs"] = resume["adapter_runs"]
        report["thinking_check"] = resume["thinking_check"]
    else:
        for selector in ("fast", "standard"):
            actual = configured(settings, resolve(settings, selector))
            model = budget.factory(actual)
            items = [{"role": "user", "content": "请查询FitLife支持哪些运动项目。"}]
            prompt = (
                "先调用get_activity_catalog，category为all；"
                "拿到结果后只列出返回的中文运动名，不重复调用。"
            )
            try:
                first = await complete(model, prompt, items, tools, actual, ledger, trace, 1)
                assert len(first.calls) == 1 and first.calls[0]["name"] == "get_activity_catalog"
                call = first.calls[0]
                args = json.loads(call["arguments"])
                assert args == {"category": "all"}
                items.extend(first.continuation)
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": call["call_id"],
                        "output": json.dumps(get_activity_catalog(**args), ensure_ascii=False),
                    }
                )
                final = await complete(model, prompt, items, tools, actual, ledger, trace, 2)
                assert not final.calls and "步行" in final.text and "轻柔关节活动" in final.text
                report["adapter_runs"].append(
                    {
                        "profile": selector,
                        "requested_model": actual.model,
                        "response_model": final.response_model,
                        "answer": final.text,
                        "tool_arguments": args,
                    }
                )
            finally:
                await model.close()
        assert (
            report["adapter_runs"][0]["response_model"]
            != report["adapter_runs"][1]["response_model"]
        )

        actual = configured(settings, resolve(settings, "fast")).model_copy(
            update={"enable_thinking": True, "thinking_budget": 128, "max_output_tokens": 256}
        )
        model = budget.factory(actual)
        try:
            reply = await complete(
                model,
                "请回答计算结果。",
                [{"role": "user", "content": "6乘7等于多少？"}],
                [],
                actual,
                ledger,
                trace,
                1,
            )
            assert "42" in reply.text
            report["thinking_check"] = {
                "answer": reply.text,
                "usage": reply.usage,
                "requested_enable_thinking": True,
                "requested_thinking_budget": 128,
            }
        finally:
            await model.close()

    fixture = folder / "synthetic-walking.png"
    synthetic_card(fixture)
    actual = configured(settings, resolve(settings, "vision", capability="images"))
    model = budget.factory(actual)
    try:
        reply = await complete(
            model,
            "只抄写图片里的英文和数字，不添加建议。",
            [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_image",
                            "image_url": "data:image/png;base64,"
                            + base64.b64encode(fixture.read_bytes()).decode(),
                        },
                        {"type": "input_text", "text": "读取这张图片。"},
                    ],
                }
            ],
            [],
            actual,
            ledger,
            trace,
            1,
        )
        report["vision_check"] = {
            "fixture": fixture.name,
            "response_model": reply.response_model,
            "answer": reply.text,
            "usage": reply.usage,
        }
        assert "20" in reply.text and "WALK" in reply.text.upper(), "VISION_CARD_TEXT_MISMATCH"
    finally:
        await model.close()


def api_check(budget, report):
    db = DatabaseSettings(environment="test")
    engine, actors = db.engine(), []
    try:
        migrate(engine, "test")
        actor, token = issue_token(engine)
        actors.append(actor)
        service = DomainService(engine)
        draft = service.propose_record(
            actor, "steps", {"steps": 8426}, "siliconflow-setup", today()
        )
        service.confirm(actor, draft["action_id"], draft["revision"], draft["digest"])
        before = service.lookup(actor, "steps", today(), today())
        app = create_app(
            db,
            engine=engine,
            model_factory=budget.factory,
            model_mode="siliconflow-live",
            orchestration_mode="master",
        )
        with TestClient(app) as client:
            headers = {"Authorization": "Bearer " + token, "Idempotency-Key": str(uuid4())}
            body = {"message": "查询今天步数记录，不要写入数据。"}
            response = client.post("/agent/requests", headers=headers, json=body)
            report["api_status"] = response.status_code
            assert response.status_code == 200, "SILICONFLOW_AGENT_API_FAILED"
            result = response.json()
            report["api_run_id"] = result["run_id"]
            assert result["end_reason"] == "completed" and "pending_action" not in result
            assert "8426" in result["answer"].replace(",", "").replace("，", "")
            replay = client.post("/agent/requests", headers=headers, json=body)
            assert replay.json()["run_id"] == result["run_id"]
            assert service.lookup(actor, "steps", today(), today()) == before
            report["api_check"] = {
                "answer": result["answer"],
                "request_replay_same_run": True,
                "business_facts_unchanged": True,
                "known_steps": 8426,
            }
    finally:
        clean(engine, actors)
        engine.dispose()
        report["scoped_fixture_cleanup"] = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--resume-from", help="复用已通过的文字/思考检查，仅重验图片及API")
    args = parser.parse_args()
    parent = ROOT / "artifacts/siliconflow-checks"
    parent.mkdir(parents=True, exist_ok=True)
    lock = parent / "check.lock"
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)
    folder = parent / str(uuid4())
    folder.mkdir()
    report = {
        "status": "failed",
        "created_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "budget_limit_cny": "0.5",
        "actual_billed_cny": None,
        "balance_cny": None,
    }
    env = {
        "FITLIFE_MODEL_PROFILES_FILE": str(ROOT / "config/siliconflow.models.json"),
        "FITLIFE_ROUTING_POLICY": "rules",
        "FITLIFE_ROUTING_RULES_FILE": str(ROOT / "config/routing.example.json"),
        "FITLIFE_MODEL_RETRIES": "0",
        "FITLIFE_FALLBACK_PROFILE": "",
        "FITLIFE_ROUTER_MODEL": "fast",
        "FITLIFE_PLANNER_MODEL": "standard",
        "FITLIFE_SYNTHESIS_MODEL": "standard",
    }
    saved = {k: os.environ.get(k) for k in env}
    started = time.perf_counter()
    try:
        os.environ.update(env)
        prices = public_prices()
        (folder / "prices.json").write_text(
            json.dumps(prices, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (folder / "profiles.json").write_bytes(
            (ROOT / "config/siliconflow.models.json").read_bytes()
        )
        with httpx.Client(
            timeout=30, headers={"Authorization": "Bearer " + os.environ["SILI_API_KEY"]}
        ) as client:
            balance = client.get(BASE_URL + "/user/info")
            report["balance_endpoint_status"] = balance.status_code
            response = client.get(BASE_URL + "/models")
            response.raise_for_status()
            assert IDS <= {m["id"] for m in response.json()["data"]}
        budget = CNYBudget(parent / "budget-cny-v1.json", prices)
        report["attempts_before"] = len(budget.state["attempts"])
        settings = Settings(
            fallback_profile=None,
            model_retries=0,
            max_output_tokens=256,
            max_model_calls=12,
            max_total_token_units=350000,
        )
        resume = None
        if args.resume_from:
            previous_id = (
                str(uuid4())
                if not re.fullmatch(r"[0-9a-f-]{36}", args.resume_from)
                else args.resume_from
            )
            previous_path = parent / previous_id / "report.json"
            resume = json.loads(previous_path.read_text(encoding="utf-8"))
            assert len(resume["adapter_runs"]) == 2 and "42" in resume["thinking_check"]["answer"]
            for relative in (
                "fitlife/model.py",
                "fitlife/model_profiles.py",
                "config/siliconflow.models.json",
            ):
                assert (
                    resume["source_sha256"][relative]
                    == hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
                )
            report["reused_adapter_report"] = str(previous_path.relative_to(ROOT))
        asyncio.run(asyncio.wait_for(adapter_checks(settings, budget, folder, report, resume), 180))
        api_check(budget, report)
        report["status"] = "passed"
        report["attempts_after"] = len(budget.state["attempts"])
        report["cumulative_estimated_cny"] = str(
            sum(Decimal(e.get("estimated_cny", "0")) for e in budget.state["attempts"])
        )
    except Exception as exc:
        report["error_type"] = type(exc).__name__
        if getattr(exc, "status_code", None):
            report["http_status"] = exc.status_code
        if isinstance(exc, (AssertionError, ValueError)):
            code = str(exc)
            if code and all(c.isupper() or c == "_" for c in code):
                report["error_code"] = code
    finally:
        report["seconds"] = round(time.perf_counter() - started, 3)
        report["source_sha256"] = {
            relative: hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
            for relative in (
                "fitlife/model.py",
                "fitlife/model_profiles.py",
                "fitlife/siliconflow_check.py",
                "fitlife/execution.py",
                "fitlife/circuit.py",
                "config/siliconflow.models.json",
            )
        }
        (folder / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        lock.unlink()
    print(f"硅基流动验收：{report['status']}；报告：{folder / 'report.json'}")
    if report["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
