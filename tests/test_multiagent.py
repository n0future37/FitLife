import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from fitlife.api import create_app
from fitlife.api_check import confirmation, headers
from fitlife.auth import issue_token
from fitlife.business import BusinessError, BusinessService, today
from fitlife.database import Activity, DomainRecord, PendingAction
from fitlife.domains import DomainService
from fitlife.evaluate import clean
from fitlife.model import Reply, Settings
from fitlife.model_profiles import resolve
from fitlife.routing import Plan, Requirement, check_coverage, execution_order, select_model


def test_plan_rejects_cycle_duplicate_coverage_and_wrong_tool_domain():
    from fitlife.routing import Requirements

    requirements = [Requirement(id="b", domain="body", kind="query", text="查体重")]
    with pytest.raises(ValueError):
        check_coverage(
            Plan(agents=["body"], tasks={"body": "查"}, covers={"body": ["b", "b"]}), requirements
        )
    with pytest.raises(ValueError):
        execution_order(
            Plan(
                agents=["body", "diet"],
                tasks={"body": "查", "diet": "查"},
                covers={},
                depends_on={"body": ["diet"], "diet": ["body"]},
            )
        )
    with pytest.raises(ValueError):
        Requirements(
            requirements=[
                {
                    "id": "b",
                    "domain": "body",
                    "kind": "query",
                    "text": "查步数",
                    "required_tools": ["lookup_steps_tool"],
                }
            ]
        )


def test_model_policy_checks_history_write_dependencies_and_capabilities():
    settings = Settings(routing_policy="conservative", easy_model="fast")
    plan = Plan(agents=["body"], tasks={"body": "查体重"}, covers={"body": ["b"]})
    req = [
        Requirement(
            id="b",
            domain="body",
            kind="query",
            text="查询体重",
            required_tools=["lookup_body_tool"],
        )
    ]
    assert select_model(settings, plan, "body", req, [])["model"] == "fast"
    assert (
        select_model(settings, plan, "body", req, [{"role": "user", "content": "上次"}])["model"]
        == "standard"
    )
    req = [Requirement(id="b", domain="body", kind="deliverable", text="记录体重")]
    assert select_model(settings, plan, "body", req, [])["model"] == "standard"
    assert resolve(settings, "vision", capability="images").model == settings.model
    with pytest.raises(ValueError, match="MODEL_CAPABILITY_NOT_SUPPORTED"):
        resolve(settings, "vision", capability="tools")


class MultiFixture:
    def __init__(self, events, *, fail_exercise=False, query=False):
        self.events, self.fail_exercise, self.query = events, fail_exercise, query

    async def complete(self, instructions, items, tools):
        self.events.append({"tools": sorted(t["name"] for t in tools)})
        if not tools:
            if "意图路由器" in instructions:
                reqs = [
                    {
                        "id": "b",
                        "domain": "body",
                        "kind": "query" if self.query else "deliverable",
                        "text": "查询今天体重" if self.query else "记录今天体重70kg",
                        "required_tools": ["lookup_body_tool"]
                        if self.query
                        else ["record_body_tool"],
                    },
                    {
                        "id": "e",
                        "domain": "exercise",
                        "kind": "deliverable",
                        "text": "根据身体记录及训练档案解释一般运动建议"
                        if self.query
                        else "记录今天步行20分钟",
                        "required_tools": ["lookup_training_profile_tool"]
                        if self.query
                        else ["propose_activity"],
                    },
                ]
                return Reply(text=json.dumps({"requirements": reqs}))
            if "规划三个领域" in instructions:
                return Reply(
                    text=json.dumps(
                        {
                            "agents": ["body", "exercise"],
                            "tasks": {"body": "本轮身体任务", "exercise": "本轮运动任务"},
                            "depends_on": {"exercise": ["body"]} if self.query else {},
                            "covers": {"body": ["b"], "exercise": ["e"]},
                        }
                    )
                )
            return Reply(text="依据本轮身体与运动工具结果完成汇总。候选尚未保存。")
        names = {t["name"] for t in tools}
        assert not any("confirm" in n for n in names)
        outputs = [i for i in items if i.get("type") == "function_call_output"]
        if outputs:
            return Reply(text="依据已查询结果给出一般建议；没有记录不等于没有运动。")
        day = today().isoformat()
        if "lookup_body_tool" in names:
            name = "lookup_body_tool" if self.query else "record_body_tool"
            args = (
                {"start_date": day, "end_date": day} if self.query else {"data": {"weight_kg": 70}}
            )
        else:
            if self.fail_exercise:
                raise RuntimeError("SYNTHETIC_DOMAIN_FAILURE")
            if self.query:
                payload = json.loads(items[-1]["content"])
                deps = payload["dependency_context"]
                assert deps[0]["source_agent"] == "body"
                assert deps[0]["observations"][0]["data"]["records"][0]["values"]["weight_kg"] == 70
                self.events.append({"dependency_received": True})
            name = "lookup_training_profile_tool" if self.query else "propose_activity"
            args = {} if self.query else {"data": {"activity_id": "walking", "minutes": 20}}
        call = {
            "type": "function_call",
            "name": name,
            "arguments": json.dumps(args),
            "call_id": str(uuid4()),
        }
        return Reply(calls=[call], continuation=[call])

    async def close(self):
        pass


@pytest.mark.integration
@pytest.mark.parametrize("orchestration", ["master", "master_independent"])
def test_master_bundle_confirmation_replay_privacy_and_user_isolation(test_database, orchestration):
    settings, engine = test_database
    alice, token = issue_token(engine)
    bob, other = issue_token(engine)
    events = []
    try:
        app = create_app(
            settings,
            engine=engine,
            model_factory=lambda _: MultiFixture(events),
            orchestration_mode=orchestration,
        )
        with TestClient(app) as client:
            key = uuid4()
            body = {"message": "记录今天体重70公斤、步行20分钟"}
            response = client.post("/agent/requests", headers=headers(token, key), json=body)
            assert response.status_code == 200, response.text
            result = response.json()
            pending = result["pending_action"]
            assert pending["kind"] == "action_bundle" and len(pending["payload"]["operations"]) == 2
            with Session(engine) as session:
                assert not session.scalar(
                    select(Activity).where(Activity.owner_id == str(alice.user_id))
                )
                assert not session.scalar(
                    select(DomainRecord).where(DomainRecord.owner_id == str(alice.user_id))
                )
            assert (
                client.post(
                    f"/actions/{pending['action_id']}/confirm",
                    headers=headers(other),
                    json=confirmation(pending),
                ).status_code
                == 404
            )
            for _ in range(2):
                saved = client.post(
                    f"/actions/{pending['action_id']}/confirm",
                    headers=headers(token),
                    json=confirmation(pending),
                )
                assert saved.json()["status"] == "applied"
            calls = len(events)
            replay = client.post("/agent/requests", headers=headers(token, key), json=body)
            assert replay.json()["pending_action"]["status"] == "applied" and len(events) == calls
            with Session(engine) as session:
                assert (
                    len(
                        session.scalars(
                            select(Activity).where(Activity.owner_id == str(alice.user_id))
                        ).all()
                    )
                    == 1
                )
                rows = session.scalars(
                    select(DomainRecord).where(DomainRecord.owner_id == str(alice.user_id))
                ).all()
                assert len(rows) == 1 and rows[0].values["weight_kg"] == 70
                assert not session.scalar(
                    select(DomainRecord).where(DomainRecord.owner_id == str(bob.user_id))
                )
            from pathlib import Path

            report = json.loads(
                (
                    Path(__file__).parents[1] / "artifacts/runs" / f"{result['run_id']}.json"
                ).read_text(encoding="utf-8")
            )
            assert report["data_classification"] == "private_redacted"
            assert all(
                e.get("arguments") == {"redacted": True}
                for e in report["trace"]
                if e.get("kind") == "tool"
            )
    finally:
        clean(engine, [alice, bob])


@pytest.mark.integration
def test_stale_bundle_rolls_back_earlier_operations_and_rejection_blocks_writes(test_database):
    _, engine = test_database
    actor, _ = issue_token(engine)
    service = DomainService(engine)
    try:
        pending = service.propose_record(actor, "body", {"weight_kg": 70}, str(uuid4()), today())
        service.confirm(actor, pending["action_id"], pending["revision"], pending["digest"])
        changed = service.propose_record(actor, "body", {"weight_kg": 71}, str(uuid4()), today())
        operation = {
            "kind": "domain_record",
            "payload": changed["payload"],
            "base_profile_version": 0,
            "base_plan_version": None,
        }
        bundle = service.collect_proposal(
            actor,
            [
                {
                    "kind": "record_activity",
                    "payload": {
                        "activity_id": "walking",
                        "minutes": 20,
                        "occurred_on": today().isoformat(),
                    },
                    "base_profile_version": 0,
                    "base_plan_version": None,
                },
                operation,
            ],
            str(uuid4()),
        )
        service.confirm(actor, changed["action_id"], changed["revision"], changed["digest"])
        result = service.confirm(actor, bundle["action_id"], bundle["revision"], bundle["digest"])
        assert result["status"] == "superseded"
        assert service.list_activities(actor, today(), today())["record_count"] == 0
        candidate = service.propose_record(actor, "steps", {"steps": 6000}, str(uuid4()), today())
        service.reject(actor, candidate["action_id"], candidate["revision"], candidate["digest"])
        with pytest.raises(BusinessError):
            service.confirm(
                actor, candidate["action_id"], candidate["revision"], candidate["digest"]
            )
        assert service.lookup(actor, "steps", today(), today())["records"] == []
    finally:
        clean(engine, [actor])


@pytest.mark.integration
@pytest.mark.parametrize("serial", [False, True])
def test_dependency_evidence_and_partial_failure(test_database, serial):
    settings, engine = test_database
    actor, token = issue_token(engine)
    service = DomainService(engine)
    try:
        pending = service.propose_record(actor, "body", {"weight_kg": 70}, str(uuid4()), today())
        service.confirm(actor, pending["action_id"], pending["revision"], pending["digest"])
        events = []
        app = create_app(
            settings,
            engine=engine,
            model_factory=lambda _: MultiFixture(events, query=True),
            orchestration_mode="master_serial" if serial else "master",
        )
        with TestClient(app) as client:
            result = client.post(
                "/agent/requests",
                headers=headers(token, uuid4()),
                json={"message": "根据今天身体记录和训练档案解释一般运动建议"},
            )
            assert result.status_code == 200, result.text
            assert any(e.get("dependency_received") for e in events)
        app = create_app(
            settings,
            engine=engine,
            model_factory=lambda _: MultiFixture([], fail_exercise=True),
            orchestration_mode="master",
        )
        with TestClient(app) as client:
            result = client.post(
                "/agent/requests",
                headers=headers(token, uuid4()),
                json={"message": "记录体重和运动"},
            )
            assert result.status_code == 200, result.text
            assert (
                result.json()["end_reason"] == "partial_completed"
                and "pending_action" not in result.json()
            )
        with Session(engine) as session:
            assert (
                len(
                    session.scalars(
                        select(PendingAction).where(PendingAction.owner_id == str(actor.user_id))
                    ).all()
                )
                == 1
            )
    finally:
        clean(engine, [actor])


@pytest.mark.integration
def test_food_calculation_and_shared_authoritative_profile(test_database):
    _, engine = test_database
    actor, _ = issue_token(engine)
    other, _ = issue_token(engine)
    service = DomainService(engine)
    try:
        profile = service.propose_record(
            actor,
            "training_profile",
            {"goal": "规律运动", "equipment": "哑铃"},
            str(uuid4()),
            today(),
        )
        assert profile["kind"] == "profile_patch"
        service.confirm(actor, profile["action_id"], profile["revision"], profile["digest"])
        assert BusinessService(engine).get_profile(actor)["values"]["goal"] == "规律运动"
        assert service.lookup(actor, "training_profile")["profile"]["equipment"] == "哑铃"
        food = service.propose_record(
            actor,
            "food",
            {
                "name": "合成测试食品",
                "unit": "g",
                "energy_kcal": 100,
                "protein_g": 5,
                "carbs_g": 10,
                "fat_g": 2,
                "source": "synthetic fixture",
            },
            str(uuid4()),
            today(),
        )
        saved = service.confirm(actor, food["action_id"], food["revision"], food["digest"])
        food_id = saved["result"]["record_id"]
        items = [{"food_id": food_id, "amount": 150}]
        assert service.calculate_meal(actor, items)["total"]["energy_kcal"] == 150
        with pytest.raises(BusinessError, match="FOOD_NOT_FOUND"):
            service.calculate_meal(other, items)
        meal = service.propose_record(
            actor, "meal", {"meal_type": "午餐", "items": items}, str(uuid4()), today()
        )
        assert meal["payload"]["values"]["calculation"]["total"]["energy_kcal"] == 150
        service.confirm(actor, meal["action_id"], meal["revision"], meal["digest"])
        assert (
            service.lookup(actor, "meal", today(), today())["records"][0]["values"]["calculation"][
                "total"
            ]["energy_kcal"]
            == 150
        )
    finally:
        clean(engine, [actor, other])


@pytest.mark.integration
def test_tool_budget_is_shared_before_parallel_domain_calls(test_database, monkeypatch):
    settings, engine = test_database
    actor, token = issue_token(engine)
    monkeypatch.setenv("FITLIFE_MASTER_MAX_TOOL_CALLS", "2")

    class ParallelReads(MultiFixture):
        async def complete(self, instructions, items, tools):
            if not tools and "规划三个领域" in instructions:
                return Reply(
                    text=json.dumps(
                        {
                            "agents": ["body", "exercise"],
                            "tasks": {"body": "查身体", "exercise": "查档案"},
                            "depends_on": {},
                            "covers": {"body": ["b"], "exercise": ["e"]},
                        }
                    )
                )
            if tools:
                if any(i.get("type") == "function_call_output" for i in items):
                    return Reply(text="已查询。")
                names = {t["name"] for t in tools}
                day = str(today())
                specs = (
                    [
                        ("lookup_body_tool", {"start_date": day, "end_date": day}),
                        ("lookup_body_profile_tool", {}),
                    ]
                    if "lookup_body_tool" in names
                    else [("lookup_training_profile_tool", {})]
                )
                calls = [
                    {
                        "type": "function_call",
                        "name": n,
                        "arguments": json.dumps(a),
                        "call_id": str(uuid4()),
                    }
                    for n, a in specs
                ]
                return Reply(calls=calls, continuation=calls)
            return await super().complete(instructions, items, tools)

    try:
        app = create_app(
            settings,
            engine=engine,
            model_factory=lambda _: ParallelReads([], query=True),
            orchestration_mode="master",
        )
        with TestClient(app) as client:
            response = client.post(
                "/agent/requests",
                headers=headers(token, uuid4()),
                json={"message": "分别查询身体记录和训练档案"},
            )
            assert response.status_code == 200, response.text
            assert response.json()["end_reason"] == "partial_completed"
        from pathlib import Path

        report = json.loads(
            (
                Path(__file__).parents[1] / "artifacts/runs" / f"{response.json()['run_id']}.json"
            ).read_text(encoding="utf-8")
        )
        assert sum(e.get("kind") == "tool" and e.get("executed") for e in report["trace"]) <= 2
    finally:
        clean(engine, [actor])


@pytest.mark.integration
def test_router_write_hint_cannot_block_correct_activity_candidate(test_database):
    settings, engine = test_database
    actor, token = issue_token(engine)

    class WrongWriteHint(MultiFixture):
        async def complete(self, instructions, items, tools):
            reply = await super().complete(instructions, items, tools)
            if not tools and "意图路由器" in instructions:
                inventory = json.loads(reply.text)
                inventory["requirements"][1]["required_tools"] = ["record_workout_tool"]
                return Reply(text=json.dumps(inventory))
            return reply

    try:
        with TestClient(
            create_app(
                settings,
                engine=engine,
                model_factory=lambda _: WrongWriteHint([]),
                orchestration_mode="master",
            )
        ) as client:
            response = client.post(
                "/agent/requests",
                headers=headers(token, uuid4()),
                json={"message": "记录今天体重70公斤和步行20分钟"},
            )
            assert (
                response.status_code == 200
                and response.json()["end_reason"] == "pending_confirmation"
            ), response.text
            pending = response.json()["pending_action"]
            result = client.post(
                f"/actions/{pending['action_id']}/confirm",
                headers=headers(token),
                json=confirmation(pending),
            )
            assert result.json()["status"] == "applied"
        service = DomainService(engine)
        assert service.list_activities(actor, today(), today())["recorded_minutes"] == 20
        assert len(service.lookup(actor, "body", today(), today())["records"]) == 1
    finally:
        clean(engine, [actor])
