import json
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from fitlife.adjustments import AdjustmentRequest, context, propose
from fitlife.api import create_app
from fitlife.api_check import confirmation, headers
from fitlife.auth import issue_token
from fitlife.business import BusinessError, today
from fitlife.evaluate import clean
from fitlife.model import Reply
from fitlife.plan_types import PlanRequest
from fitlife.planning import PlanningService


def apply(service, actor, action):
    return service.confirm(actor, action["action_id"], action["revision"], action["digest"])


def seed(engine, actor, week):
    service = PlanningService(engine)
    apply(
        service,
        actor,
        service.propose(
            actor,
            "profile_patch",
            dict(
                available_weekdays=[0, 2, 4],
                weekly_sessions=3,
                session_minutes=20,
                preferred_activities=["walking"],
                goal="规律活动",
            ),
            str(uuid4()),
        ),
    )
    apply(
        service,
        actor,
        service.propose_plan(
            actor, PlanRequest(week_start=week, expected_plan_version=0), str(uuid4())
        ),
    )
    return service


def request_for(service, actor, week, **options):
    facts = context(service, actor, week, options.get("reference_week_start"))
    return AdjustmentRequest(
        week_start=week,
        expected_plan_version=facts["plans"][str(week)]["version"],
        expected_evidence_digest=facts["evidence_digest"],
        **options,
    )


@pytest.mark.integration
def test_adjustment_http_confirmation_replay_ownership_and_difference(test_database):
    settings, engine = test_database
    actor, token = issue_token(engine)
    other, other_token = issue_token(engine)
    monday = today() - timedelta(days=today().weekday())
    week = monday + timedelta(days=7)
    service = seed(engine, actor, week)
    try:
        request = request_for(
            service,
            actor,
            week,
            reference_week_start=monday,
            available_weekdays=[1, 5],
            remaining_sessions=2,
            session_minutes=15,
        )
        with TestClient(create_app(settings, engine=engine)) as client:
            key = uuid4()
            response = client.post(
                "/plans/adjustment-actions",
                headers=headers(token, key),
                json=request.model_dump(mode="json"),
            )
            assert response.status_code == 200, response.text
            candidate = response.json()
            assert candidate["payload"]["adjustment"]["changes"]
            assert service.get_plan(actor, week)["version"] == 1
            assert (
                client.post(
                    f"/actions/{candidate['action_id']}/confirm",
                    headers=headers(other_token),
                    json=confirmation(candidate),
                ).status_code
                == 404
            )
            for _ in range(2):
                saved = client.post(
                    f"/actions/{candidate['action_id']}/confirm",
                    headers=headers(token),
                    json=confirmation(candidate),
                )
                assert saved.json()["status"] == "applied"
            assert (
                client.post(
                    "/plans/adjustment-actions",
                    headers=headers(token, key),
                    json=request.model_dump(mode="json"),
                ).json()["action_id"]
                == candidate["action_id"]
            )
        plan = service.get_plan(actor, week)
        assert plan["version"] == 2
        assert [(s["day"], s["minutes"]) for s in plan["plan"]["sessions"]] == [
            (str(week + timedelta(days=d)), 15) for d in [1, 5]
        ]
        assert service.get_profile(actor)["values"]["session_minutes"] == 20
    finally:
        clean(engine, [actor, other])


@pytest.mark.integration
@pytest.mark.parametrize("change", ["activity", "profile", "plan"])
def test_changed_evidence_blocks_old_adjustment(test_database, change):
    _, engine = test_database
    actor, _ = issue_token(engine)
    week = today() - timedelta(days=today().weekday()) + timedelta(days=7)
    service = seed(engine, actor, week)
    try:
        request = request_for(
            service, actor, week, reference_week_start=week - timedelta(days=7), session_minutes=15
        )
        candidate = propose(service, actor, request, str(uuid4()))
        if change == "activity":
            apply(
                service,
                actor,
                service.propose(
                    actor,
                    "record_activity",
                    dict(activity_id="walking", occurred_on=str(today()), minutes=5),
                    str(uuid4()),
                ),
            )
        elif change == "profile":
            apply(
                service,
                actor,
                service.propose(actor, "profile_patch", dict(goal="调整后的目标"), str(uuid4())),
            )
        else:
            apply(
                service,
                actor,
                service.propose_plan(
                    actor,
                    PlanRequest(week_start=week, expected_plan_version=1, session_minutes=25),
                    str(uuid4()),
                ),
            )
        assert apply(service, actor, candidate)["status"] == "superseded"
        assert service.get_plan(actor, week)["plan"]["sessions"][0]["minutes"] == (
            25 if change == "plan" else 20
        )
    finally:
        clean(engine, [actor])


@pytest.mark.integration
def test_past_sessions_locked_and_all_future_cancel_supported(test_database):
    _, engine = test_database
    actor, _ = issue_token(engine)
    week = today() - timedelta(days=today().weekday())
    service = seed(engine, actor, week)
    try:
        before = service.get_plan(actor, week)["plan"]["sessions"]
        action = propose(
            service, actor, request_for(service, actor, week, remaining_sessions=0), str(uuid4())
        )
        if action["status"] == "unchanged":
            assert all(s["day"] <= str(today()) for s in before)
        else:
            after = action["payload"]["sessions"]
            assert after == [s for s in before if s["day"] <= str(today())]
            assert apply(service, actor, action)["status"] == "applied"
        future = week + timedelta(days=7)
        empty = propose(
            service, actor, request_for(service, actor, future, remaining_sessions=0), str(uuid4())
        )
        assert empty["status"] == "unchanged"  # 无安排无需创建空变更。
    finally:
        clean(engine, [actor])


@pytest.mark.integration
def test_stale_context_and_rejection_prevent_plan_write(test_database):
    _, engine = test_database
    actor, _ = issue_token(engine)
    week = today() - timedelta(days=today().weekday()) + timedelta(days=7)
    service = seed(engine, actor, week)
    try:
        request = request_for(service, actor, week, session_minutes=15)
        candidate = propose(service, actor, request, str(uuid4()))
        service.reject(actor, candidate["action_id"], candidate["revision"], candidate["digest"])
        with pytest.raises(BusinessError, match="ACTION_NOT_PENDING"):
            apply(service, actor, candidate)
        apply(
            service,
            actor,
            service.propose(actor, "profile_patch", dict(goal="新目标"), str(uuid4())),
        )
        with pytest.raises(BusinessError, match="ADJUSTMENT_CONTEXT_STALE"):
            propose(service, actor, request, str(uuid4()))
        assert service.get_plan(actor, week)["version"] == 1
    finally:
        clean(engine, [actor])


class AdjustmentModel:
    def __init__(self, week):
        self.week = str(week)

    async def complete(self, instructions, items, tools):
        if not tools:
            if "意图路由器" in instructions:
                return Reply(
                    text=json.dumps(
                        {
                            "requirements": [
                                dict(
                                    id="a1",
                                    domain="exercise",
                                    text="按记录调整计划",
                                    kind="deliverable",
                                    required_tools=[
                                        "get_plan_adjustment_context",
                                        "query_activities",
                                        "get_current_plan",
                                    ],
                                )
                            ]
                        }
                    )
                )
            return Reply(text="已依据实际记录与本轮安排生成调整，尚未保存。")
        used = sum(x.get("type") == "function_call_output" for x in items)
        name, args = (
            ("get_plan_adjustment_context", dict(week_start=self.week))
            if not used
            else (
                "propose_plan_adjustment",
                dict(
                    week_start=self.week,
                    options=dict(
                        remaining_sessions=2, available_weekdays=[1, 5], session_minutes=15
                    ),
                ),
            )
        )
        call = dict(
            type="function_call", name=name, arguments=json.dumps(args), call_id=str(uuid4())
        )
        return Reply(calls=[call], continuation=[call], response_model="adjustment-fixed-v1")

    async def close(self):
        pass


@pytest.mark.integration
def test_master_agent_adjustment_uses_real_mcp_and_confirmed_bundle(test_database):
    settings, engine = test_database
    actor, token = issue_token(engine)
    week = today() - timedelta(days=today().weekday()) + timedelta(days=7)
    service = seed(engine, actor, week)
    try:
        with TestClient(
            create_app(
                settings,
                engine=engine,
                model_factory=lambda _: AdjustmentModel(week),
                orchestration_mode="master",
                synthetic_evaluation=True,
            )
        ) as client:
            response = client.post(
                "/agent/requests",
                headers=headers(token, uuid4()),
                json={"message": "根据记录把下周计划改到周二周六各15分钟，共两次"},
            )
            assert response.status_code == 200, response.text
            assert response.json()["end_reason"] == "pending_confirmation", response.text
            action = response.json()["pending_action"]
            assert action["payload"]["operations"][0]["kind"] == "adjust_plan"
            assert service.get_plan(actor, week)["version"] == 1
            assert apply(service, actor, action)["status"] == "applied"
            assert service.get_plan(actor, week)["version"] == 2
    finally:
        clean(engine, [actor])
