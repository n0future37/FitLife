"""One decision loop across all current domain tools; same trusted business services."""

import time
from dataclasses import replace
from datetime import date, timedelta

from fitlife.agent import execute
from fitlife.candidates import finalize_candidates
from fitlife.model_profiles import resolve
from fitlife.registry import COMMON_PROMPT, DOMAIN_PROMPTS, DOMAIN_TOOLS

UNIFIED_VERSION = "unified-v1"
UNIFIED_TOOLS = frozenset().union(*DOMAIN_TOOLS.values())
DOMAIN_RULES = "\n".join(
    prompt.removeprefix(COMMON_PROMPT) for prompt in DOMAIN_PROMPTS.values()
).replace(
    "本领域不更改height_cm/age/sex/diet_preferences，不为仅修改目标生成计划。",
    "身体字段使用身体档案工具、饮食偏好使用饮食工具、运动目标使用propose_profile_update；不为仅修改目标生成计划。",
)
UNIFIED_PROMPT = (
    COMMON_PROMPT.replace(
        "你是FitLife的领域子Agent。只完成当前领域任务，事实以本轮真实工具结果为准。",
        "你是FitLife统一单Agent，负责本轮身体、饮食、运动的全部任务，事实以本轮真实工具结果为准。",
    )
    + "你在同一个工具循环中完成全部领域，不调用路由器或子Agent。"
    + "每个领域本轮至多一项候选，总共最多三项。每次收到candidate_collected仅说明暂存候选；"
    + "必须继续完成原题其余领域和要求，全部完成后再结束回答。不能确认或声称已保存。"
    + "身体、饮食、运动的分工说明如下，它们都是你的职责；不得漏掉任何一项原始要求。"
    + DOMAIN_RULES
)


async def execute_unified(message, model, settings, trace, context, *, ledger):
    resolve(settings, "standard", capability="tools")
    started = time.perf_counter()
    request_day = date.fromisoformat(context.server_env["FITLIFE_TRUSTED_REQUEST_DAY"])
    monday = request_day - timedelta(days=request_day.weekday())
    dates = (
        f"本轮上海日期：{request_day}；本周星期一：{monday}；"
        f"下周星期一：{monday + timedelta(days=7)}。以原始请求为准，历史不改变今天。"
    )
    proposals, evidence = [], []
    trace.append(
        {"kind": "unified_start", "version": UNIFIED_VERSION, "catalog_tools": len(UNIFIED_TOOLS)}
    )
    local = replace(
        context,
        allowed_tools=UNIFIED_TOOLS,
        instructions=UNIFIED_PROMPT + dates,
        prompt_version=UNIFIED_VERSION,
        proposal_sink=proposals,
        evidence_sink=evidence,
        required_tools=frozenset(),
        server_env={**context.server_env, "FITLIFE_COLLECT_CANDIDATES": "1"},
    )
    result = await execute(message, model, settings, trace, local, ledger=ledger)
    failed = any(e["status"] == "error" for e in evidence)
    response = {**result, "orchestration_version": UNIFIED_VERSION}
    if failed:
        response.update(
            end_reason="partial_completed",
            answer=result["answer"] + "\n\n必要检索未完成，本轮未生成可确认的整份候选，也未保存。",
        )
    elif result["end_reason"] == "completed" and proposals:
        response = await finalize_candidates(
            proposals, response, context, settings, trace, ledger, answer_prefix=result["answer"]
        )
    trace.append({"kind": "unified_finish", "collected": len(proposals), "failed_read": failed})
    response["steps"] = ledger.steps
    response["total_seconds"] = round(time.perf_counter() - started, 3)
    return response
