"""显式免费云验收：同一密钥、两个模型、规则选择与真实只读工具往返。"""

import argparse
import asyncio
import hashlib
import json
import os
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx

from fitlife.execution import Ledger, complete
from fitlife.model import Settings, create_model
from fitlife.model_profiles import configured, resolve
from fitlife.routing import Plan, Requirement, select_model
from fitlife.tools import get_activity_catalog

ROOT = Path(__file__).resolve().parents[1]
BASE_URL = "https://openrouter.ai/api/v1"
PRICE_CAP = {"prompt": 0, "completion": 0, "request": 0, "image": 0}


def account_snapshot(client):
    response = client.get(BASE_URL + "/key")
    response.raise_for_status()
    data = response.json()["data"]
    return {k: data.get(k) for k in ("usage", "is_free_tier", "free_model_daily_requests")}


async def verify(settings, report, maximum):
    ledger, trace = Ledger(), []
    tools = [
        {
            "type": "function",
            "name": "get_activity_catalog",
            "description": "查询FitLife应用实际支持的运动项目；只读，无数据库写入。",
            "parameters": {
                "type": "object",
                "properties": {"category": {"type": "string", "enum": ["all"]}},
                "required": ["category"],
                "additionalProperties": False,
            },
        }
    ]
    requirements = [Requirement(id="q", domain="exercise", kind="query", text="查询支持的运动")]
    plan = Plan(agents=["exercise"], tasks={"exercise": "查询"}, covers={"exercise": ["q"]})
    runs = []

    async def guard(request):
        if (request.url.scheme, request.url.host, request.url.path) != (
            "https",
            "openrouter.ai",
            "/api/v1/chat/completions",
        ):
            raise ValueError("FREE_CHECK_ENDPOINT_REJECTED")
        body = json.loads(request.content)
        permitted = {resolve(settings, p).model for p in ("fast", "standard")}
        if body.get("model") not in permitted or not body["model"].endswith(":free"):
            raise ValueError("FREE_CHECK_MODEL_REJECTED")
        provider = body.get("provider", {})
        if provider.get("max_price") != PRICE_CAP or provider.get("require_parameters") is not True:
            raise ValueError("FREE_CHECK_PRICE_CAP_REQUIRED")
        if body.get("models") or body.get("plugins") or body.get("route"):
            raise ValueError("FREE_CHECK_FALLBACK_OR_PLUGIN_REJECTED")
        if len(report["network_requests"]) >= maximum:
            raise ValueError("FREE_CHECK_CALL_BUDGET_EXHAUSTED")
        report["network_requests"].append({"model": body["model"], "price_cap": PRICE_CAP})

    async def observe(response):
        await response.aread()
        event = report["network_requests"][-1]
        event["http_status"] = response.status_code
        if response.is_success:
            data = response.json()
            event.update(
                response_model=data.get("model"),
                provider=data.get("provider"),
                usage=data.get("usage"),
            )
        else:
            # Do not retain provider error text, echoed headers or credentials.
            event["error_code"] = response.status_code
            try:
                message = json.dumps(response.json().get("error", {})).lower()
                event["error_category"] = (
                    "upstream_rate_limit"
                    if "upstream" in message and "rate" in message
                    else "privacy_policy"
                    if "privacy" in message or "data policy" in message
                    else "credit_limit"
                    if "credit" in message or "balance" in message
                    else "rate_limit"
                    if response.status_code == 429
                    else "provider_error"
                )
            except (ValueError, AttributeError):
                event["error_category"] = "non_json_error"

    for name, history in (("fresh-query", []), ("query-with-history", [{}])):
        route = select_model(settings, plan, "exercise", requirements, history)
        selected = configured(settings, resolve(settings, route["model"]))
        assert selected.free_only and selected.fallback_profile is None
        model = create_model(selected)
        model.client._client.event_hooks = {"request": [guard], "response": [observe]}
        items = [{"role": "user", "content": "查询FitLife实际支持哪些运动。"}]
        instructions = (
            "你是FitLife助手。必须先调用get_activity_catalog，category为all。"
            "拿到工具结果后，只用中文列出它返回的运动名称；不添加其他项目，不重复调用。"
        )
        event = {"scenario": name, "route": route, "requested_model": selected.model}
        runs.append(event)
        report["runs"] = runs
        started = time.perf_counter()
        try:
            first = await complete(model, instructions, items, tools, selected, ledger, trace, 1)
            assert len(first.calls) == 1, "EXPECTED_ONE_READ_ONLY_TOOL"
            call = first.calls[0]
            assert call["name"] == "get_activity_catalog", "UNEXPECTED_TOOL"
            arguments = json.loads(call["arguments"])
            assert arguments == {"category": "all"}, "UNEXPECTED_TOOL_ARGUMENTS"
            tool_result = get_activity_catalog(**arguments)
            event["tool_call"] = {"name": call["name"], "arguments": arguments}
            event["tool_result"] = tool_result
            items.extend(first.continuation)
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": call["call_id"],
                    "output": json.dumps(tool_result, ensure_ascii=False),
                }
            )
            final = await complete(model, instructions, items, tools, selected, ledger, trace, 2)
            assert not final.calls, "REPEATED_TOOL_CALL"
            assert "步行" in final.text and "轻柔关节活动" in final.text, (
                "ANSWER_MISSING_TOOL_FACTS"
            )
            assert final.response_model, "RESPONSE_MODEL_ID_MISSING"
            event.update(status="passed", response_model=final.response_model, answer=final.text)
        finally:
            event["seconds"] = round(time.perf_counter() - started, 3)
            await model.close()
            report["trace"] = trace
    assert runs[0]["requested_model"] != runs[1]["requested_model"]
    assert runs[0]["response_model"] != runs[1]["response_model"], "RESPONSE_IDS_DID_NOT_SWITCH"
    report["same_key_two_models"] = True
    report["business_writes"] = 0
    for event in report["network_requests"]:
        cost = (event.get("usage") or {}).get("cost")
        if cost is not None:
            assert Decimal(str(cost)) == 0, "NONZERO_REPORTED_COST"
    report["status"] = "passed"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-free", action="store_true", required=True)
    parser.add_argument("--max-calls", type=int, choices=range(4, 9), default=6)
    args = parser.parse_args()
    folder = ROOT / "artifacts/openrouter-free" / str(uuid4())
    folder.mkdir(parents=True)
    for relative in ("config/openrouter.free.models.json", "config/openrouter.free.routing.json"):
        (folder / Path(relative).name).write_bytes((ROOT / relative).read_bytes())
    report = {
        "status": "failed",
        "created_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "credential_source": "OPENROUTER_API_KEY",
        "network_requests": [],
        "paid_fallback_allowed": False,
        "local_fallback_allowed": False,
        "scope": "project-router-adapter-readonly-tool; not full-master/database evaluation",
    }
    path = folder / "report.json"
    try:
        key = os.environ.get("OPENROUTER_API_KEY")
        if not key:
            raise ValueError("MISSING_OPENROUTER_API_KEY")
        settings = Settings(
            model_profiles_file=ROOT / "config/openrouter.free.models.json",
            routing_rules_file=ROOT / "config/openrouter.free.routing.json",
            routing_policy="rules",
            fallback_profile=None,
            model_retries=0,
            max_model_calls=args.max_calls,
            max_output_tokens=1536,
            circuit_enabled=False,
            request_timeout=45,
        )
        with httpx.Client(timeout=30, follow_redirects=False) as client:
            response = client.get(BASE_URL + "/models")
            response.raise_for_status()
            catalog = {m["id"]: m for m in response.json()["data"]}
            selected = {}
            for name in ("fast", "standard"):
                profile = resolve(settings, name)
                model = catalog[profile.model]
                pricing = model["pricing"]
                assert all(Decimal(str(v)) == 0 for v in pricing.values() if v is not None)
                assert "tools" in model.get("supported_parameters", [])
                assert profile.free_only and profile.api_family == "chat"
                selected[name] = {
                    "model": profile.model,
                    "pricing": pricing,
                    "parameters": model.get("supported_parameters", []),
                }
            report["public_catalog"] = selected
            client.headers["Authorization"] = "Bearer " + key
            report["account_before"] = account_snapshot(client)
            try:
                asyncio.run(asyncio.wait_for(verify(settings, report, args.max_calls), 150))
            finally:
                report["account_after"] = account_snapshot(client)
            before, after = (
                report["account_before"].get("usage"),
                report["account_after"].get("usage"),
            )
            if before is not None and after is not None:
                delta = Decimal(str(after)) - Decimal(str(before))
                report["account_usage_delta_usd"] = str(delta)
                assert delta == 0, "ACCOUNT_USAGE_CHANGED"
    except Exception as exc:
        report.update(status="failed", error_type=type(exc).__name__)
        if isinstance(exc, httpx.HTTPStatusError):
            report["http_status"] = exc.response.status_code
        elif getattr(exc, "status_code", None):
            report["http_status"] = exc.status_code
        # Only expose our uppercase assertion codes; never stringify SDK errors.
        if isinstance(exc, (AssertionError, ValueError)):
            code = str(exc)
            if code and all(c.isupper() or c == "_" for c in code):
                report["error_code"] = code
    finally:
        files = (
            "fitlife/model.py",
            "fitlife/model_profiles.py",
            "fitlife/openrouter_check.py",
            "fitlife/routing.py",
            "config/openrouter.free.models.json",
            "config/openrouter.free.routing.json",
        )
        report["source_sha256"] = {
            relative: hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
            for relative in files
        }
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"OpenRouter免费验收：{report['status']}；报告：{path}")
    if report["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
