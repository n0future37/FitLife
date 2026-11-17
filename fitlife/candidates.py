"""Shared trusted candidate merge/persistence; models never receive confirmation tools."""

import json
import sys
from dataclasses import replace

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from fitlife.agent import PROJECT_ROOT, call_tool, candidate_response
from fitlife.execution import ExecutionError


async def finalize_candidates(
    proposals, response, context, settings, trace, ledger, *, answer_prefix=None
):
    patches = [op for op in proposals if op["kind"] == "profile_patch"]
    if len(patches) > 1:
        merged = {}
        for op in patches:
            if any(k in merged and merged[k] != v for k, v in op["payload"].items()):
                return {
                    **response,
                    "answer": ("不同领域的画像变更存在冲突，请明确希望保存的值。本轮未生成候选。"),
                }
            if op["base_profile_version"] != patches[0]["base_profile_version"]:
                raise ExecutionError("PROFILE_VERSION_CONFLICT")
            merged.update(op["payload"])
        proposals = [op for op in proposals if op["kind"] != "profile_patch"]
        proposals.append({**patches[0], "payload": merged})
    # Only a trusted controller persists the bundle; models cannot confirm facts.
    env = {k: v for k, v in context.server_env.items() if k != "FITLIFE_COLLECT_CANDIDATES"}
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", context.server_module],
        cwd=str(PROJECT_ROOT),
        env={"PYTHONIOENCODING": "utf-8", **env},
    )
    if ledger.tool_calls >= settings.max_tool_calls:
        raise ExecutionError("TOOL_BUDGET_EXHAUSTED")
    ledger.tool_calls += 1
    root_context = replace(context, allowed_tools=frozenset({"propose_action_bundle"}))
    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            candidate = await call_tool(
                session,
                {
                    "name": "propose_action_bundle",
                    "arguments": json.dumps({"operations": proposals}, ensure_ascii=False),
                    "call_id": "master-bundle",
                },
                root_context,
                settings,
                trace,
                ledger.steps,
            )
    if "action_id" not in candidate:
        if candidate.get("error_code") == "BUNDLE_CONSTRAINT_CONFLICT":
            return {
                **response,
                "answer": (
                    "临时约束与计划草案冲突，本轮未生成可确认候选，也未保存。"
                    "请明确本次需要例外还是调整计划。"
                ),
                "termination_code": "NEEDS_CLARIFICATION",
            }
        raise ExecutionError("BUNDLE_PERSIST_FAILED")
    response.update(candidate_response(candidate, ledger.steps))
    if answer_prefix is not None:
        response["answer"] = answer_prefix + "\n\n" + response["answer"]
    return response
