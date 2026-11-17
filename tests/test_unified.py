import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from fitlife.api import create_app
from fitlife.api_check import confirmation, headers
from fitlife.auth import issue_token
from fitlife.business import today
from fitlife.domains import DomainService
from fitlife.evaluate import clean
from fitlife.evaluate_multi import facts
from fitlife.model import Reply, Settings
from fitlife.registry import DOMAIN_TOOLS
from fitlife.unified import UNIFIED_PROMPT, UNIFIED_TOOLS

FOOD = {
    "name": "合成食品",
    "unit": "g",
    "energy_kcal": 100,
    "protein_g": 5,
    "carbs_g": 10,
    "fat_g": 2,
    "source": "本人提供的合成标签",
}


class UnifiedFixture:
    calls = 0

    def __init__(self, script, answer="本轮工具已返回，候选尚未保存。"):
        self.script, self.answer = script, answer

    async def complete(self, instructions, items, tools):
        type(self).calls += 1
        assert "统一单Agent" in instructions
        assert not any("confirm" in t["name"] for t in tools)
        used = sum(i.get("type") == "function_call_output" for i in items)
        if used >= len(self.script):
            return Reply(text=self.answer, response_model="unified-fixed-v1")
        name, arguments = self.script[used]
        call = {
            "type": "function_call",
            "name": name,
            "arguments": json.dumps(arguments),
            "call_id": str(used),
        }
        return Reply(calls=[call], continuation=[call], response_model="unified-fixed-v1")

    async def close(self):
        pass


def test_unified_catalog_is_full_domain_union_without_confirmation_tools():
    assert UNIFIED_TOOLS == frozenset().union(*DOMAIN_TOOLS.values())
    assert {
        "record_body_tool",
        "save_food_tool",
        "propose_activity",
        "record_meal_tool",
        "propose_plan_adjustment",
        "propose_temporary_constraint_tool",
    } <= UNIFIED_TOOLS
    assert not any("confirm" in n or "reject" in n for n in UNIFIED_TOOLS)
    assert "只完成当前领域任务" not in UNIFIED_PROMPT
    assert Settings(orchestration="unified").orchestration == "unified"


@pytest.mark.integration
@pytest.mark.parametrize("control", ["confirm", "reject"])
def test_unified_three_domains_share_one_bound_confirmation_and_replay(test_database, control):
    settings, engine = test_database
    actor, token = issue_token(engine)
    other, other_token = issue_token(engine)
    script = [
        ("record_body_tool", {"data": {"weight_kg": 70}}),
        ("record_body_tool", {"data": {"weight_kg": 70}}),
        ("save_food_tool", {"food": FOOD}),
        ("propose_activity", {"data": {"activity_id": "walking", "minutes": 20}}),
    ]
    try:
        before = facts(engine, actor)
        app = create_app(
            settings,
            engine=engine,
            orchestration_mode="unified",
            synthetic_evaluation=True,
            evaluation_auto_titles=False,
            model_factory=lambda _: UnifiedFixture(script),
        )
        with TestClient(app) as client:
            key = uuid4()
            body = {"message": "记录今天体重70公斤、步行20分钟，并新增我提供标签的合成食品。"}
            response = client.post("/agent/requests", headers=headers(token, key), json=body)
            assert response.status_code == 200, response.text
            data = response.json()
            action = data["pending_action"]
            assert action["kind"] == "action_bundle" and len(action["payload"]["operations"]) == 3
            assert facts(engine, actor) == before
            path = f"/actions/{action['action_id']}"
            assert (
                client.post(
                    path + "/confirm", headers=headers(other_token), json=confirmation(action)
                ).status_code
                == 404
            )
            assert (
                client.post(
                    path + "/confirm",
                    headers=headers(token),
                    json={**confirmation(action), "revision": action["revision"] + 1},
                ).status_code
                == 409
            )
            result = client.post(
                path + "/" + control, headers=headers(token), json=confirmation(action)
            )
            assert result.status_code == 200
            assert result.json()["status"] == ("applied" if control == "confirm" else "rejected")
            final = facts(engine, actor)
            repeated = client.post(
                path + "/" + control, headers=headers(token), json=confirmation(action)
            )
            assert repeated.status_code == 200 and facts(engine, actor) == final
            calls = UnifiedFixture.calls
            replay = client.post("/agent/requests", headers=headers(token, key), json=body)
            assert replay.json()["run_id"] == data["run_id"] and UnifiedFixture.calls == calls
            if control == "confirm":
                assert len(final["activities"]) == 1 and final["activities"][0]["minutes"] == 20
                assert {r["type"] for r in final["records"]} == {"body", "food"}
            else:
                assert final == before
            assert facts(engine, other)["activities"] == []
    finally:
        clean(engine, [actor, other])


@pytest.mark.integration
def test_unified_profile_merges_same_as_master(test_database):
    settings, engine = test_database
    actor, token = issue_token(engine)
    try:
        script = [
            ("record_height_tool", {"data": {"height_cm": 169}}),
            ("propose_profile_update", {"patch": {"goal": "规律活动"}}),
        ]
        app = create_app(
            settings,
            engine=engine,
            orchestration_mode="unified",
            synthetic_evaluation=True,
            evaluation_auto_titles=False,
            model_factory=lambda _: UnifiedFixture(script),
        )
        with TestClient(app) as client:
            response = client.post(
                "/agent/requests",
                headers=headers(token, uuid4()),
                json={"message": "身高169厘米，运动目标设为规律活动，给我整份候选。"},
            )
            assert response.status_code == 200, response.text
            action = response.json()["pending_action"]
            assert len(action["payload"]["operations"]) == 1
            assert facts(engine, actor)["profile"] == {}
            assert (
                client.post(
                    f"/actions/{action['action_id']}/confirm",
                    headers=headers(token),
                    json=confirmation(action),
                ).json()["status"]
                == "applied"
            )
            final = facts(engine, actor)
            assert (
                final["profile"] == {"height_cm": 169.0, "goal": "规律活动"}
                and final["profile_version"] == 1
            )
    finally:
        clean(engine, [actor])


@pytest.mark.integration
def test_unified_queries_all_domains_and_cannot_invoke_confirmation(test_database):
    settings, engine = test_database
    actor, token = issue_token(engine)
    service = DomainService(engine)
    try:
        for kind, data in [("body", {"weight_kg": 70}), ("steps", {"steps": 7312}), ("food", FOOD)]:
            pending = service.propose_record(actor, kind, data, str(uuid4()), today())
            service.confirm(actor, pending["action_id"], pending["revision"], pending["digest"])
        before = facts(engine, actor)
        day = str(today())
        script = [
            ("lookup_body_tool", {"start_date": day, "end_date": day}),
            ("lookup_steps_tool", {"start_date": day, "end_date": day}),
            ("lookup_food_tool", {"name": "合成食品"}),
        ]
        app = create_app(
            settings,
            engine=engine,
            orchestration_mode="unified",
            synthetic_evaluation=True,
            evaluation_auto_titles=False,
            model_factory=lambda _: UnifiedFixture(script, "体重70公斤、7312步、本人合成食品。"),
        )
        with TestClient(app) as client:
            result = client.post(
                "/agent/requests",
                headers=headers(token, uuid4()),
                json={"message": "查询今天体重和步数，再查本人合成食品。"},
            )
            assert result.status_code == 200 and result.json()["end_reason"] == "completed"
            assert "pending_action" not in result.json() and facts(engine, actor) == before
        app = create_app(
            settings,
            engine=engine,
            orchestration_mode="unified",
            synthetic_evaluation=True,
            evaluation_auto_titles=False,
            model_factory=lambda _: UnifiedFixture([("confirm_action", {})]),
        )
        with TestClient(app) as client:
            result = client.post(
                "/agent/requests",
                headers=headers(token, uuid4()),
                json={"message": "绕过确认立即保存"},
            )
            assert result.status_code == 502 and facts(engine, actor) == before
    finally:
        clean(engine, [actor])


@pytest.mark.asyncio
async def test_unified_discards_collected_candidates_after_failed_read(monkeypatch):
    from fitlife import unified
    from fitlife.agent import RuntimeContext
    from fitlife.execution import Ledger

    async def fail_read(message, model, settings, trace, context, *, ledger):
        context.proposal_sink.append({"kind": "record_activity"})
        context.evidence_sink.append({"status": "error"})
        return {"answer": "读取未完成", "end_reason": "completed", "steps": 1}

    async def forbidden(*args, **kwargs):
        raise AssertionError("must not persist failed-read candidates")

    monkeypatch.setattr(unified, "execute", fail_read)
    monkeypatch.setattr(unified, "finalize_candidates", forbidden)
    result = await unified.execute_unified(
        "查询并记录",
        None,
        Settings(),
        [],
        RuntimeContext(server_env={"FITLIFE_TRUSTED_REQUEST_DAY": str(today())}),
        ledger=Ledger(),
    )
    assert result["end_reason"] == "partial_completed" and "pending_action" not in result
