import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from fitlife import agent
from fitlife.agent import RuntimeContext
from fitlife.business import ProfilePatch
from fitlife.execution import ExecutionError, Ledger
from fitlife.intent_constraints import explicit_weekdays, weekday_conflict
from fitlife.model import Reply, Settings
from fitlife.profile_inputs import EXERCISE_FIELDS, ExerciseProfilePatch, ToolProfilePatch


def test_profile_tool_schema_matches_accepted_values_and_domain_scope():
    schema = ExerciseProfilePatch.model_json_schema()
    assert set(schema["properties"]) == EXERCISE_FIELDS
    assert schema["minProperties"] == 1 and schema["additionalProperties"] is False
    for p in schema["properties"].values():
        assert p.get("default", "not-provided") is not None
        assert not any(v.get("type") == "null" for v in p.get("anyOf", []))
    assert ExerciseProfilePatch(goal="规律活动").model_dump(exclude_unset=True) == {
        "goal": "规律活动"
    }
    for value in ({"goal": None}, {"height_cm": 169}, {}, {"goal": ""}):
        with pytest.raises(ValueError):
            ExerciseProfilePatch.model_validate(value)
    assert ToolProfilePatch(height_cm=169).height_cm == 169  # single-agent compatibility
    assert "height_cm" in ToolProfilePatch.model_json_schema()["properties"]
    assert "goal" in ProfilePatch.model_fields  # authoritative business type unchanged


def test_weekday_guard_is_bound_to_clear_user_constraints_not_inferred_values():
    assert explicit_weekdays("只在星期六步行12分钟") == (5,)
    assert explicit_weekdays("只安排星期二和星期六两次") == (1, 5)
    assert explicit_weekdays("仅在星期日运动") == (6,)
    for text in (
        "之前只在周六，现在改为周日",
        "不要只在周六",
        "周六不能去",
        "只在周六不训练",
        "请制定计划",
    ):
        assert explicit_weekdays(text) is None
    call = {
        "name": "propose_plan_adjustment",
        "arguments": json.dumps({"options": {"available_weekdays": [6]}}),
    }
    assert weekday_conflict("只在星期六步行", call)["expected_available_weekdays"] == [5]
    assert weekday_conflict("只在星期日步行", call) is None
    assert (
        weekday_conflict("只在星期六步行", {**call, "arguments": '{"options":null}'})["error_code"]
        == "EXPLICIT_WEEKDAY_CONFLICT"
    )


@asynccontextmanager
async def pipe(_):
    yield None, None


class Session:
    calls = []
    error = False

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
                SimpleNamespace(
                    name="propose_week_plan", description="", inputSchema={"type": "object"}
                )
            ]
        )

    async def call_tool(self, name, arguments):
        self.calls.append(arguments)
        return SimpleNamespace(
            isError=self.error, content=[], structuredContent={"proposal": {"kind": "save_plan"}}
        )


class Model:
    def __init__(self, correct):
        self.correct, self.count = correct, 0

    async def complete(self, *_):
        self.count += 1
        args = {"overrides": {"available_weekdays": [5 if self.count > 1 and self.correct else 6]}}
        call = {
            "type": "function_call",
            "name": "propose_week_plan",
            "arguments": json.dumps(args),
            "call_id": str(self.count),
        }
        return Reply(calls=[call], continuation=[call])


@pytest.mark.asyncio
async def test_wrong_weekday_is_rejected_before_tool_then_can_be_corrected(monkeypatch):
    monkeypatch.setattr(agent, "stdio_client", pipe)
    monkeypatch.setattr(agent, "ClientSession", Session)
    monkeypatch.setattr(Session, "calls", [])
    monkeypatch.setattr(Session, "error", False)
    ctx = RuntimeContext(
        allowed_tools=frozenset({"propose_week_plan"}), original_request="只安排周六运动"
    )
    trace, ledger = [], Ledger()
    result = await agent.execute(
        "wrapper is not trusted authority", Model(True), Settings(), trace, ctx, ledger=ledger
    )
    assert result["end_reason"] == "proposed" and ledger.steps == 2
    assert Session.calls == [{"overrides": {"available_weekdays": [5]}}]
    assert any(e["kind"] == "tool_argument_blocked" and not e["executed"] for e in trace)


@pytest.mark.asyncio
async def test_repeated_invalid_arguments_fail_fast_without_second_tool_call(monkeypatch):
    monkeypatch.setattr(agent, "stdio_client", pipe)
    monkeypatch.setattr(agent, "ClientSession", Session)
    monkeypatch.setattr(Session, "calls", [])
    monkeypatch.setattr(Session, "error", True)
    ctx = RuntimeContext(allowed_tools=frozenset({"propose_week_plan"}))
    with pytest.raises(ExecutionError, match="REPEATED_INVALID_TOOL_ARGUMENTS"):
        await agent.execute("query", Model(False), Settings(), [], ctx)
    assert len(Session.calls) == 1
