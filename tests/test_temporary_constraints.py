from datetime import timedelta
from uuid import uuid4

import pytest

from fitlife import conversations, requests
from fitlife.auth import issue_token
from fitlife.business import BusinessService, TemporaryConstraint, today
from fitlife.constraints import applicable, memory_scope
from fitlife.evaluate import clean
from fitlife.plan_types import PlanRequest
from fitlife.planning import PlanningService


def rule():
    monday = today() - timedelta(days=today().weekday())
    return {
        "name": "本周限时",
        "domains": ["exercise"],
        "start_date": str(monday),
        "end_date": str(monday + timedelta(days=6)),
        "note": "本周每次最多20分钟",
        "max_session_minutes": 20,
    }


def test_expiry_changes_memory_scope_and_invalid_ranges_are_rejected():
    values = {"temporary_constraints": [rule()]}
    day = today()
    assert applicable(values, day, day, "exercise")
    assert applicable(values, day, day, "diet") == []
    next_week = day + timedelta(days=7)
    assert applicable(values, next_week, next_week) == []
    assert memory_scope(1, values, day) != memory_scope(1, values, next_week)
    with pytest.raises(ValueError):
        TemporaryConstraint.model_validate({**rule(), "end_date": "2000-01-01"})


@pytest.mark.integration
def test_plan_constraint_explicit_exception_and_future_expiry(test_database):
    _, engine = test_database
    actor, _ = issue_token(engine)
    service = PlanningService(engine)
    try:
        pending = service.propose(
            actor,
            "profile_patch",
            {
                "preferred_activities": ["walking"],
                "available_weekdays": [0, 2],
                "weekly_sessions": 2,
                "session_minutes": 30,
                "temporary_constraints": [rule()],
            },
            str(uuid4()),
        )
        service.confirm(actor, pending["action_id"], pending["revision"], pending["digest"])
        monday = today() - timedelta(days=today().weekday())
        request = PlanRequest(week_start=monday, expected_plan_version=0)
        assert service.propose_plan(actor, request, str(uuid4()))["status"] == "constraint_conflict"
        pending = service.propose_plan(
            actor, request.model_copy(update={"session_minutes": 20}), str(uuid4())
        )
        assert pending["payload"]["temporary_constraints_applied"] == ["本周限时"]
        service.confirm(actor, pending["action_id"], pending["revision"], pending["digest"])
        exception = PlanRequest(
            week_start=monday,
            expected_plan_version=1,
            session_minutes=30,
            temporary_constraint_exceptions=("本周限时",),
        )
        pending = service.propose_plan(actor, exception, str(uuid4()))
        assert pending["payload"]["temporary_constraint_exceptions"] == ["本周限时"]
        assert (
            service.confirm(actor, pending["action_id"], pending["revision"], pending["digest"])[
                "status"
            ]
            == "applied"
        )
        assert (
            service.get_profile(actor)["values"]["temporary_constraints"][0]["max_session_minutes"]
            == 20
        )
        future = service.propose_plan(
            actor,
            PlanRequest(week_start=monday + timedelta(days=7), expected_plan_version=0),
            str(uuid4()),
        )
        assert (
            future["payload"]["temporary_constraints_applied"] == []
            and future["payload"]["sessions"][0]["minutes"] == 30
        )
    finally:
        clean(engine, [actor])


@pytest.mark.integration
def test_expired_constraint_is_not_resent_in_conversation_history(test_database, monkeypatch):
    _, engine = test_database
    actor, _ = issue_token(engine)
    service = BusinessService(engine)
    try:
        pending = service.propose(
            actor, "profile_patch", {"temporary_constraints": [rule()]}, str(uuid4())
        )
        service.confirm(actor, pending["action_id"], pending["revision"], pending["digest"])
        id_ = conversations.create(engine, actor)["conversation_id"]
        first = requests.claim(engine, actor, uuid4(), "本周限时要求", 30, id_)
        requests.finish(
            engine,
            actor,
            first["id"],
            first["nonce"],
            {"answer": "已查询当前约束", "end_reason": "completed"},
            True,
        )
        second = requests.claim(engine, actor, uuid4(), "继续按本周要求", 30, id_)
        assert len(second["history"]) == 2
        requests.finish(
            engine,
            actor,
            second["id"],
            second["nonce"],
            {"answer": "查询完成", "end_reason": "completed"},
            True,
        )
        monkeypatch.setattr(requests, "today", lambda: today() + timedelta(days=7))
        after = requests.claim(engine, actor, uuid4(), "本周如何安排", 30, id_)
        assert after["history"] == []
        assert service.get_profile(actor)["version"] == 1
    finally:
        clean(engine, [actor])


@pytest.mark.integration
def test_bundle_cannot_save_plan_conflicting_with_its_new_constraint(test_database):
    from fitlife.business import BusinessError

    _, engine = test_database
    actor, _ = issue_token(engine)
    service = PlanningService(engine)
    try:
        profile = service.propose(
            actor,
            "profile_patch",
            {
                "preferred_activities": ["walking"],
                "available_weekdays": [0],
                "weekly_sessions": 1,
                "session_minutes": 30,
            },
            str(uuid4()),
        )
        service.confirm(actor, profile["action_id"], profile["revision"], profile["digest"])
        monday = today() - timedelta(days=today().weekday())
        service.collect_candidates = True
        proposal = service.propose_plan(
            actor, PlanRequest(week_start=monday, expected_plan_version=0), str(uuid4())
        )["proposal"]
        service.collect_candidates = False
        operations = [
            proposal,
            {
                "kind": "profile_patch",
                "payload": {"temporary_constraints": [rule()]},
                "base_profile_version": 1,
                "base_plan_version": None,
            },
        ]
        with pytest.raises(BusinessError, match="BUNDLE_CONSTRAINT_CONFLICT"):
            service.propose(actor, "action_bundle", {"operations": operations}, str(uuid4()))
        assert service.get_plan(actor, monday)["version"] == 0
    finally:
        clean(engine, [actor])
