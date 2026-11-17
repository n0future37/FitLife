import asyncio
import copy
import json

import pytest

from fitlife import agent, master
from fitlife.agent import RuntimeContext
from fitlife.experiment_contracts import tool_contract_matches
from fitlife.routing import has_complex_intent


def test_v2_plan_equivalence_accepts_inheritance_without_masking_explicit_errors():
    wanted = {
        "tool": "propose_week_plan",
        "arguments": {
            "week_start": "2026-11-09",
            "overrides": {
                "activity_id": "walking",
                "available_weekdays": [1, 5],
                "weekly_sessions": 2,
                "session_minutes": 15,
            },
        },
    }
    read = {
        "name": "get_user_profile",
        "finished_ns": 10,
        "result": {
            "version": 1,
            "values": {
                "preferred_activities": ["walking"],
                "available_weekdays": [1, 5],
                "weekly_sessions": 2,
                "session_minutes": 15,
            },
        },
    }
    call = {
        "name": "propose_week_plan",
        "started_ns": 20,
        "arguments": {"week_start": "2026-11-09", "overrides": {"session_minutes": 15}},
        "result": {"status": "proposed", "proposal": {"payload": {"source_profile_version": 1}}},
    }
    checks = {"expected_final_state": True, "critical_order": True}
    assert not tool_contract_matches(wanted, [read, call], checks, "strict-v1")
    assert tool_contract_matches(wanted, [read, call], checks, "profile-equivalent-v2")
    for key, value in (
        ("session_minutes", 20),
        ("activity_id", "cycling"),
        ("available_weekdays", [1, 1, 5]),
    ):
        bad = copy.deepcopy(call)
        bad["arguments"]["overrides"][key] = value
        assert not tool_contract_matches(wanted, [read, bad], checks, "profile-equivalent-v2")
    for reads in (
        [],
        [{**read, "finished_ns": 30}],
        [{**read, "result": {**read["result"], "version": 2}}],
    ):
        assert not tool_contract_matches(wanted, [*reads, call], checks, "profile-equivalent-v2")
    explicit = copy.deepcopy(call)
    explicit["arguments"]["overrides"] = {
        **wanted["arguments"]["overrides"],
        "available_weekdays": [5, 1],
    }
    assert tool_contract_matches(wanted, [explicit], checks, "profile-equivalent-v2")
    assert not tool_contract_matches(
        wanted, [read, call], {**checks, "expected_final_state": False}, "profile-equivalent-v2"
    )


def test_direct_negation_does_not_hide_positive_complex_intent():
    assert not has_complex_intent("查询步数，不做建议，不需要分析")
    assert has_complex_intent("查询步数，需要分析，但不要建议")
    assert has_complex_intent("不要取消分析")
    assert has_complex_intent("查询计划，不做建议")


@pytest.mark.asyncio
async def test_external_run_cancellation_flushes_ledger_then_propagates(monkeypatch, tmp_path):
    class Stub:
        async def close(self):
            pass

    async def slow(*_, ledger, **__):
        ledger.steps, ledger.calls = 2, 1
        await asyncio.Event().wait()

    monkeypatch.setattr(master, "execute_master", slow)
    monkeypatch.setattr(agent, "PROJECT_ROOT", tmp_path)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            agent.run(
                "private input",
                context=RuntimeContext(orchestration="master"),
                model_factory=lambda _: Stub(),
            ),
            timeout=0.1,
        )
    report = json.loads(
        next((tmp_path / "artifacts/runs").glob("*.json")).read_text(encoding="utf-8")
    )
    assert report["end_reason"] == "cancelled" and report["termination_code"] == "RUN_CANCELLED"
    assert report["steps"] == 2 and report["model_attempts"] == 1
    assert "private input" not in json.dumps(report)
