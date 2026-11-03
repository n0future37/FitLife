"""周计划/进度模块集成验收：只用测试库与合成数据，可选真实模型。"""

import argparse
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Barrier
from uuid import uuid4

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from fastapi.testclient import TestClient
from sqlalchemy import delete, event, select
from sqlalchemy.orm import Session

from fitlife.api import create_app
from fitlife.api_check import confirmation, headers
from fitlife.auth import issue_token
from fitlife.business import BusinessError, today
from fitlife.credentials import database_password
from fitlife.database import (
    AccessToken,
    Activity,
    AgentRequest,
    Base,
    DatabaseSettings,
    PendingAction,
    Plan,
    PlanRevision,
    Profile,
    User,
)
from fitlife.db_check import expect_error
from fitlife.manage import migrate
from fitlife.model import Reply
from fitlife.plan_types import PlanRequest
from fitlife.planning import PlanningService

ROOT = Path(__file__).resolve().parents[1]


def apply(service, actor, candidate):
    return service.confirm(
        actor, candidate["action_id"], candidate["revision"], candidate["digest"]
    )


class DependencyFixture:
    def __init__(self, week):
        self.week = week
        self.step = 0

    async def complete(self, instructions, items, tools):
        self.step += 1
        name = "get_current_plan" if self.step == 2 else "propose_week_plan"
        arguments = {"week_start": str(self.week)}
        if name == "propose_week_plan":
            arguments["overrides"] = {"session_minutes": 12}
        call = {
            "type": "function_call",
            "name": name,
            "arguments": json.dumps(arguments),
            "call_id": f"fixture-{self.step}",
        }
        return Reply(calls=[call], continuation=[call], response_model="plan-fixture-v1")

    async def close(self):
        pass


def verify(engine, settings, report, live):
    actors = []
    tokens = []
    service = PlanningService(engine)
    week = today() - timedelta(days=today().weekday() + 7)
    try:
        for _ in range(3):
            actor, token = issue_token(engine)
            actors.append(actor)
            tokens.append(token)
        alice, bob, live_actor = actors

        def propose(minutes, version, key):
            return service.propose_plan(
                alice,
                PlanRequest(
                    week_start=week, expected_plan_version=version, session_minutes=minutes
                ),
                key,
            )

        missing = service.propose_plan(
            bob, PlanRequest(week_start=week, expected_plan_version=0), "missing"
        )
        assert missing["status"] == "needs_information" and set(missing["missing_fields"]) == {
            "available_weekdays",
            "weekly_sessions",
            "session_minutes",
            "preferred_activities",
        }
        profile = service.propose(
            alice,
            "profile_patch",
            {
                "goal": "规律运动",
                "preferred_activities": ["walking"],
                "available_weekdays": [0, 2, 4],
                "weekly_sessions": 3,
                "session_minutes": 20,
            },
            "profile",
        )
        apply(service, alice, profile)
        request = PlanRequest(week_start=week, expected_plan_version=0)
        draft = service.propose_plan(alice, request, "plan-first")
        assert service.get_plan(alice, week)["version"] == 0 and draft["base_plan_version"] == 0
        assert [s["minutes"] for s in draft["payload"]["sessions"]] == [20, 20, 20]
        expect_error("ACTION_NOT_FOUND", lambda: apply(service, bob, draft))
        apply(service, alice, draft)
        assert service.get_plan(alice, week)["version"] == 1
        assert service.propose_plan(alice, request, "plan-first")["status"] == "applied"
        assert (
            service.propose_plan(
                alice, PlanRequest(week_start=week, expected_plan_version=1), "unchanged"
            )["status"]
            == "unchanged"
        )
        report["checks"].append("missing_information_initial_confirmation_and_replay")

        rejected = propose(15, 1, "rejected")
        service.reject(alice, rejected["action_id"], rejected["revision"], rejected["digest"])
        expect_error("ACTION_NOT_PENDING", lambda: apply(service, alice, rejected))
        first, stale = propose(30, 1, "first"), propose(10, 1, "stale")
        apply(service, alice, first)
        assert apply(service, alice, stale)["status"] == "superseded"
        assert service.get_plan(alice, week)["plan"]["sessions"][0]["minutes"] == 30
        report["checks"].append("rejection_and_plan_version_conflict")
        edited = propose(20, 2, "edited")
        payload = {
            **edited["payload"],
            "sessions": [{**s, "minutes": 25} for s in edited["payload"]["sessions"]],
        }
        revision = service.revise(
            alice, edited["action_id"], edited["revision"], edited["digest"], payload
        )
        expect_error("STALE_CONFIRMATION", lambda: apply(service, alice, edited))
        apply(service, alice, revision)
        assert service.get_plan(alice, week)["version"] == 3
        assert service.get_profile(alice)["values"]["session_minutes"] == 20
        invalidated = propose(30, 3, "profile-stale")
        apply(
            service,
            alice,
            service.propose(alice, "profile_patch", {"session_minutes": 10}, "profile-change"),
        )
        assert apply(service, alice, invalidated)["status"] == "superseded"
        report["checks"].append("modified_candidate_old_confirmation_and_profile_binding")

        for i, (day, activity, minutes) in enumerate(
            [(0, "walking", 15), (1, "walking", 40), (0, "gentle_mobility", 30)]
        ):
            activity_draft = service.propose(
                alice,
                "record_activity",
                {
                    "activity_id": activity,
                    "occurred_on": str(week + timedelta(days=day)),
                    "minutes": minutes,
                },
                f"activity-{i}",
            )
            apply(service, alice, activity_draft)
        progress = service.get_progress(alice, week)
        assert (
            progress["planned_minutes"],
            progress["matched_minutes"],
            progress["recorded_minutes"],
        ) == (75, 15, 85)
        assert progress["completion_percent"] == 20 and progress["extra_recorded_minutes"] == 70
        assert progress["completed_sessions"] == 0 and progress["partial_sessions"] == 1
        apply(
            service,
            alice,
            service.propose(
                alice,
                "record_activity",
                {"activity_id": "walking", "occurred_on": str(week), "minutes": 20},
                "activity-extra",
            ),
        )
        progress = service.get_progress(alice, week)
        assert progress["matched_minutes"] == 25 and progress["completion_percent"] == 33.33
        assert progress["recorded_minutes"] == 105 and progress["activity_record_count"] == 4
        assert progress["completed_sessions"] == 1 and progress["partial_sessions"] == 0
        assert service.get_progress(bob, week)["status"] == "no_plan"
        report["checks"].append("same_day_activity_matching_cap_partial_and_unplanned_minutes")

        fault = propose(20, 3, "rollback")

        def fail(mapper, connection, target):
            raise RuntimeError("INJECTED_PLAN_FAILURE")

        event.listen(PlanRevision, "before_insert", fail)
        try:
            try:
                apply(service, alice, fault)
            except RuntimeError as exc:
                assert str(exc) == "INJECTED_PLAN_FAILURE"
            else:
                raise AssertionError("Fault not injected")
        finally:
            event.remove(PlanRevision, "before_insert", fail)
        assert service.get_plan(alice, week)["version"] == 3
        assert service.get_action(alice, fault["action_id"])["status"] == "pending"
        apply(service, alice, fault)
        pending = propose(15, 4, "concurrent")
        barrier = Barrier(2)

        def concurrent(_):
            barrier.wait(timeout=10)
            return apply(service, alice, pending)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(concurrent, range(2)))
        assert results[0]["result"] == results[1]["result"]
        saved = service.get_plan(alice, week)
        assert saved["version"] == 5
        with Session(engine) as session:
            assert (
                len(
                    session.scalars(
                        select(PlanRevision).where(PlanRevision.plan_id == saved["plan_id"])
                    ).all()
                )
                == 5
            )
        report["checks"].append("atomic_plan_history_rollback_and_concurrent_single_revision")

        api_week = week - timedelta(days=7)
        app = create_app(
            settings,
            orchestration_mode="single",
            engine=engine,
            model_factory=lambda _: DependencyFixture(api_week),
        )
        with TestClient(app) as client:
            assert client.get(f"/plans/{api_week}").status_code == 401
            assert (
                client.get(
                    f"/plans/{api_week + timedelta(days=1)}", headers=headers(tokens[0])
                ).status_code
                == 422
            )
            draft = client.post(
                "/plans/actions",
                headers=headers(tokens[0], uuid4()),
                json={
                    "week_start": str(api_week),
                    "expected_plan_version": 0,
                    "session_minutes": 20,
                },
            ).json()
            assert draft["kind"] == "save_plan"
            assert (
                client.post(
                    f"/actions/{draft['action_id']}/confirm",
                    headers=headers(tokens[1]),
                    json=confirmation(draft),
                ).status_code
                == 404
            )
            assert (
                client.post(
                    f"/actions/{draft['action_id']}/confirm",
                    headers=headers(tokens[0]),
                    json=confirmation(draft),
                ).json()["status"]
                == "applied"
            )
            response = client.post(
                "/agent/requests",
                headers=headers(tokens[0], uuid4()),
                json={"message": "创建验收计划"},
            )
            assert (
                response.status_code == 200
                and response.json()["end_reason"] == "pending_confirmation"
            )
            run_id = response.json()["run_id"]
            trace = json.loads(
                (ROOT / "artifacts/runs" / f"{run_id}.json").read_text(encoding="utf-8")
            )["trace"]
            steps = [t for t in trace if t["kind"] == "tool"]
            assert steps[0]["business_status"] == "dependency_required"
            assert (
                steps[1]["name"] == "get_current_plan" and steps[2]["business_status"] == "pending"
            )
            assert service.get_plan(alice, api_week)["plan"]["sessions"][0]["minutes"] == 20
        report["checks"].append("authenticated_plan_api_and_mcp_read_dependency_recovery")

        if live:
            current_week = today() - timedelta(days=today().weekday())
            live_profile = service.propose(
                live_actor,
                "profile_patch",
                {
                    "goal": "建立规律运动习惯",
                    "preferred_activities": ["walking"],
                    "available_weekdays": [today().weekday()],
                    "weekly_sessions": 1,
                    "session_minutes": 20,
                },
                "live-profile",
            )
            apply(service, live_actor, live_profile)
            os.environ.setdefault("FITLIFE_REASONING_EFFORT", "low")
            with TestClient(
                create_app(settings, orchestration_mode="single", engine=engine)
            ) as client:
                message = (
                    f"请按我已保存的运动画像生成{current_week}开始的一周计划草案，不修改画像。"
                )
                response = client.post(
                    "/agent/requests",
                    headers=headers(tokens[2], uuid4()),
                    json={"message": message},
                )
                assert (
                    response.status_code == 200
                    and response.json()["end_reason"] == "pending_confirmation"
                )
                draft = response.json()["pending_action"]
                assert draft["kind"] == "save_plan" and len(draft["payload"]["sessions"]) == 1
                assert draft["payload"]["sessions"][0] == {
                    "day": str(today()),
                    "activity_id": "walking",
                    "minutes": 20,
                }
                run_id = response.json()["run_id"]
                trace = json.loads(
                    (ROOT / "artifacts/runs" / f"{run_id}.json").read_text(encoding="utf-8")
                )["trace"]
                steps = [t for t in trace if t["kind"] == "tool"]
                read = next(i for i, t in enumerate(steps) if t["name"] == "get_current_plan")
                created = next(
                    i
                    for i, t in enumerate(steps)
                    if t["name"] == "propose_week_plan" and t["business_status"] == "pending"
                )
                assert read < created
                assert service.get_plan(live_actor, current_week)["version"] == 0
                assert (
                    client.post(
                        f"/actions/{draft['action_id']}/confirm",
                        headers=headers(tokens[2]),
                        json=confirmation(draft),
                    ).json()["status"]
                    == "applied"
                )
                activity = client.post(
                    "/activities/actions",
                    headers=headers(tokens[2], uuid4()),
                    json={"activity_id": "walking", "minutes": 15},
                ).json()
                client.post(
                    f"/actions/{activity['action_id']}/confirm",
                    headers=headers(tokens[2]),
                    json=confirmation(activity),
                ).raise_for_status()
                expected = service.get_progress(live_actor, current_week)
                assert expected["completion_percent"] == 75 and expected["matched_minutes"] == 15
                query = (
                    f"查询{current_week}开始这一周的计划完成进度，"
                    "说明记录时长、计划时长和匹配完成率，不修改计划。"
                )
                response = client.post(
                    "/agent/requests", headers=headers(tokens[2], uuid4()), json={"message": query}
                )
                assert response.status_code == 200 and response.json()["end_reason"] == "completed"
                result = response.json()
                run2 = result["run_id"]
                trace = json.loads(
                    (ROOT / "artifacts/runs" / f"{run2}.json").read_text(encoding="utf-8")
                )["trace"]
                assert any(t.get("name") == "get_plan_progress" for t in trace)
                report["live_runs"] = [run_id, run2]
                report["synthetic_answer_review"] = {
                    "input": query,
                    "answer": result["answer"],
                    "expected": {
                        "planned_minutes": 20,
                        "matched_minutes": 15,
                        "completion_percent": 75,
                    },
                    "review_status": "pending",
                }
            report["checks"].append("live_plan_candidate_confirm_and_progress_tool")
    finally:
        owners = [str(a.user_id) for a in actors]
        with Session(engine) as session, session.begin():
            plan_ids = select(Plan.id).where(Plan.owner_id.in_(owners))
            session.execute(delete(PlanRevision).where(PlanRevision.plan_id.in_(plan_ids)))
            for table in (AccessToken, AgentRequest, Plan, Activity, Profile, PendingAction):
                session.execute(delete(table).where(table.owner_id.in_(owners)))
            session.execute(delete(User).where(User.id.in_(owners)))
        with Session(engine) as session:
            for table in (AccessToken, AgentRequest, Plan, Activity, Profile, PendingAction):
                assert session.scalar(select(table).where(table.owner_id.in_(owners))) is None
            assert session.scalar(select(User).where(User.id.in_(owners))) is None
        report["fixture_cleanup"] = "verified"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    settings = DatabaseSettings(
        environment="test", password=database_password("test", interactive=False)
    )
    engine = settings.engine()
    report = {"status": "failed", "checks": [], "live_requested": args.live, "phase": "migration"}
    try:
        migrate(engine, "test")
        with engine.connect() as connection:
            diff = compare_metadata(
                MigrationContext.configure(
                    connection,
                    opts={
                        "version_table": "fl_alembic_version",
                        "include_object": lambda obj, name, kind, reflected, other: (
                            kind != "table" or name in Base.metadata.tables
                        ),
                    },
                ),
                Base.metadata,
            )
            assert not diff, "ORM and migration differ"
        report["phase"] = "plan_checks"
        verify(engine, settings, report, args.live)
        report["status"], report["phase"] = "passed", "completed"
    except Exception as exc:
        report["error_type"] = type(exc).__name__
        if isinstance(exc, BusinessError):
            report["error_code"] = exc.code
        driver_args = getattr(getattr(exc, "orig", None), "args", ())
        if driver_args and isinstance(driver_args[0], int):
            report["database_error_number"] = driver_args[0]
    finally:
        engine.dispose()
    report["source_sha256"] = {
        str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (ROOT / "fitlife").glob("*.py")
    }
    folder = ROOT / "artifacts/plan-checks"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{uuid4()}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"计划模块验收：{report['status']}；报告：{path}")
    if report["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
