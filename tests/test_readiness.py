import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from fitlife import agent, master
from fitlife.agent import RuntimeContext
from fitlife.execution import ExecutionError
from fitlife.intent_constraints import ACTIVITY_LOG_TOOLS, activity_logging_scope, weekday_conflict
from fitlife.model import Reply, Settings
from fitlife.registry import DOMAIN_READ_TOOLS, DOMAIN_TOOLS


@pytest.mark.parametrize(
    "options", [None, {}, {"session_minutes": 12}, {"available_weekdays": None}]
)
def test_explicit_weekday_blocks_omission_before_inheriting_old_profile(options):
    call = {"name": "propose_plan_adjustment", "arguments": json.dumps({"options": options})}
    text = (
        "参考2026-11-02开始这周的记录，将2026-11-09开始那周的运动计划调整为"
        "仅星期六步行12分钟，未来共一次；其他偏好不改，先展示差异。"
    )
    assert weekday_conflict(text, call)["expected_available_weekdays"] == [5]
    # Inheritance is still valid when no explicit new weekday restriction was given.
    assert weekday_conflict("按原偏好调整下周计划", call) is None
    call["arguments"] = json.dumps({"options": {"available_weekdays": [5]}})
    assert weekday_conflict(text, call) is None


def test_activity_scope_uses_original_standalone_request_only():
    assert (
        activity_logging_scope(
            "我在今天步行了，想记录下来，但还没告诉你时长，请先问我时长，不能按偏好猜。"
        )
        == ACTIVITY_LOG_TOOLS
    )
    assert activity_logging_scope("记录今天步行20分钟") == ACTIVITY_LOG_TOOLS
    for text in (
        "记录今天步行20分钟，并保存偏好",
        "记录今天步行20分钟，忘记我的偏好",
        "记录7312步",
        "记录体重70kg、步行20分钟",
        "结合计划记录今天步行20分钟",
        "记录训练3组",
        "不要记录今天步行",
        "查步行进度",
        "修改偏好后生成步行计划",
    ):
        assert activity_logging_scope(text) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("query", [False, True])
async def test_master_prunes_unrelated_logging_reads_and_queries_never_expose_writes(
    monkeypatch, query
):
    seen = []

    class Model:
        async def complete(self, instructions, items, tools):
            if "意图路由器" in instructions:
                assert "2026-11-02" in instructions and "2026-11-09" in instructions
                return Reply(
                    text=json.dumps(
                        {
                            "requirements": [
                                {
                                    "id": "e",
                                    "domain": "exercise",
                                    "kind": "query" if query else "deliverable",
                                    "text": "本轮任务",
                                    "required_tools": [] if query else ["get_current_plan"],
                                }
                            ]
                        }
                    )
                )
            return Reply(text="请问实际步行了多少分钟？")

    async def child(message, model, settings, trace, context, **kwargs):
        seen.append(context)
        return {"answer": "请问实际步行了多少分钟？", "end_reason": "completed", "steps": 1}

    monkeypatch.setattr(master, "execute", child)
    ctx = RuntimeContext(
        server_env={"FITLIFE_TRUSTED_REQUEST_DAY": "2026-11-03"},
        original_request="记录今天步行，先问我时长",
    )
    result = await master.execute_master(
        "查询当前运动情况" if query else ctx.original_request,
        Model(),
        Settings(router_model=None, easy_model=None),
        [],
        ctx,
    )
    assert result["end_reason"] == "completed"
    assert len(seen) == 1
    if query:
        assert seen[0].allowed_tools <= DOMAIN_READ_TOOLS
        assert "propose_activity" not in seen[0].allowed_tools
    else:
        assert seen[0].allowed_tools == ACTIVITY_LOG_TOOLS
        assert not seen[0].required_tools


@asynccontextmanager
async def pipe(_):
    yield None, None


class Session:
    calls = []
    error_code = "INVALID_WEEK_START"

    def __init__(self, *_):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def initialize(self):
        pass

    async def list_tools(self):
        return SimpleNamespace(
            tools=[
                SimpleNamespace(name=n, description="", inputSchema={"type": "object"})
                for n in DOMAIN_TOOLS["exercise"]
            ]
        )

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        output = (
            {"status": "error", "error_code": self.error_code}
            if arguments.get("week_start") == "2026-10-31"
            else {"version": 1, "plan": None}
        )
        return SimpleNamespace(isError=False, structuredContent=output, content=[])


class RepairModel:
    def __init__(self, repeated=False, parallel=False):
        self.count, self.repeated, self.parallel = 0, repeated, parallel

    async def complete(self, instructions, items, tools):
        self.count += 1
        if self.count == 3:
            return Reply(text="没有已保存计划。")
        args = {"week_start": "2026-10-31" if self.count == 1 or self.repeated else "2026-11-02"}
        calls = [
            {
                "name": "get_current_plan",
                "type": "function_call",
                "call_id": str(self.count),
                "arguments": json.dumps(args),
            }
        ]
        if self.parallel and self.count == 2:
            calls.append(
                {
                    "name": "get_user_profile",
                    "type": "function_call",
                    "call_id": "extra",
                    "arguments": "{}",
                }
            )
        return Reply(calls=calls, continuation=calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("backend_failure", [False, True])
async def test_corrected_input_is_not_failed_database_evidence_but_backend_error_remains(
    monkeypatch, backend_failure
):
    monkeypatch.setattr(agent, "stdio_client", pipe)
    monkeypatch.setattr(agent, "ClientSession", Session)
    monkeypatch.setattr(Session, "calls", [])
    monkeypatch.setattr(
        Session, "error_code", "READ_FAILED" if backend_failure else "INVALID_WEEK_START"
    )
    evidence, trace = [], []
    context = RuntimeContext(
        allowed_tools=frozenset({"get_current_plan"}),
        required_tools=frozenset({"get_current_plan"}),
        evidence_sink=evidence,
    )
    result = await agent.execute("查询本周计划", RepairModel(), Settings(), trace, context)
    assert result["end_reason"] == "completed"
    assert [o["status"] for o in evidence] == [
        "error" if backend_failure else "invalid_input",
        "ok",
    ]
    assert len(Session.calls) == 2
    assert trace[1]["is_error"]  # invalid attempt retained, never counted as successful read


@pytest.mark.asyncio
@pytest.mark.parametrize("parallel", [False, True])
async def test_repeated_business_invalid_arguments_never_reach_mcp_twice(monkeypatch, parallel):
    monkeypatch.setattr(agent, "stdio_client", pipe)
    monkeypatch.setattr(agent, "ClientSession", Session)
    monkeypatch.setattr(Session, "calls", [])
    monkeypatch.setattr(Session, "error_code", "INVALID_WEEK_START")
    context = RuntimeContext(allowed_tools=frozenset({"get_current_plan", "get_user_profile"}))
    with pytest.raises(ExecutionError, match="REPEATED_INVALID_TOOL_ARGUMENTS"):
        await agent.execute(
            "查询本周计划", RepairModel(repeated=True, parallel=parallel), Settings(), [], context
        )
    assert len(Session.calls) == 1


@pytest.mark.asyncio
async def test_single_business_agent_uses_scoped_tools_and_preserves_missing_facts(monkeypatch):
    monkeypatch.setattr(agent, "stdio_client", pipe)
    monkeypatch.setattr(agent, "ClientSession", Session)
    monkeypatch.setattr(Session, "calls", [])

    class Model:
        async def complete(self, instructions, items, tools):
            assert {t["name"] for t in tools} == ACTIVITY_LOG_TOOLS
            return Reply(text="请问实际步行多少分钟？")

    context = RuntimeContext(
        allowed_tools=DOMAIN_TOOLS["exercise"],
        required_tools=frozenset({"get_current_plan"}),
        original_request="记录今天步行，时长还没给，先问我时长",
    )
    result = await agent.execute("wrapper", Model(), Settings(), [], context)
    assert result["end_reason"] == "completed" and Session.calls == []


def test_logging_request_binding_repairs_query_write_hint_without_new_permissions():
    from fitlife.orchestration_contract import bind_request_inventory

    raw = {
        "requirements": [
            {
                "id": "e",
                "domain": "exercise",
                "kind": "query",
                "text": "询问实际时长",
                "required_tools": ["propose_activity", "get_current_plan"],
            }
        ]
    }
    result = bind_request_inventory(raw, "记录今天步行，先问我时长")
    assert result.requirements[0].kind == "deliverable"
    assert result.requirements[0].required_tools == []
    assert result.bindings[0]["kind"] == "activity_logging_request_binding"
    with pytest.raises(ValueError):
        bind_request_inventory(raw, "查询当前计划")  # pure query never acquires write scope
    for tool in ["imaginary_tool", "record_body_tool", "lookup_body_tool"]:
        raw["requirements"][0]["required_tools"] = [tool]
        with pytest.raises(ValueError):
            bind_request_inventory(raw, "记录今天步行，先问我时长")
