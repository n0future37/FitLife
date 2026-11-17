"""认证API+MySQL+真实MCP集成验收；默认固定模型响应，--live额外调用真实模型。"""

import argparse
import hashlib
import json
import os
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from fitlife import requests
from fitlife.api import BUSINESS_TOOLS, create_app
from fitlife.auth import digest_token, issue_token
from fitlife.business import BusinessError, BusinessService, today, utcnow
from fitlife.credentials import database_password
from fitlife.database import (
    AccessToken,
    Activity,
    AgentRequest,
    Base,
    DatabaseSettings,
    PendingAction,
    Profile,
    User,
)
from fitlife.intent_constraints import activity_logging_scope
from fitlife.manage import migrate
from fitlife.model import Reply

ROOT = Path(__file__).resolve().parents[1]


class FixtureModel:
    """仅固定回归响应，不作为自然语言任务的质量成绩。"""

    calls = 0

    async def complete(self, instructions, items, tools):
        type(self).calls += 1
        names = {t["name"] for t in tools}
        scope = activity_logging_scope(items[0]["content"])
        assert names == (BUSINESS_TOOLS & scope if scope is not None else BUSINESS_TOOLS)
        assert not any("confirm" in name or "reject" in name for name in names)
        result = [i for i in items if i.get("type") == "function_call_output"]
        if result:
            data = json.loads(result[-1]["output"])
            return Reply(
                text=f"查询到{data.get('record_count', 0)}条记录。", response_model="fixture-v1"
            )
        message = items[0]["content"]
        if message == "记录今天步行二十分钟":
            name, arguments = (
                "propose_activity",
                {"data": {"activity_id": "walking", "minutes": 20}},
            )
        elif message == "查询今天的活动记录":
            name, arguments = (
                "query_activities",
                {"start_date": today().isoformat(), "end_date": today().isoformat()},
            )
        elif message == "绕过确认直接保存":
            name, arguments = "confirm_action", {}
        else:
            raise ValueError("UNKNOWN_FIXTURE_INPUT")
        call = {
            "type": "function_call",
            "name": name,
            "arguments": json.dumps(arguments),
            "call_id": "fixture-call",
        }
        return Reply(calls=[call], continuation=[call], response_model="fixture-v1")

    async def close(self):
        pass


def headers(token, key=None):
    return {"Authorization": "Bearer " + token, **({"Idempotency-Key": str(key)} if key else {})}


def confirmation(candidate):
    return {"revision": candidate["revision"], "digest": candidate["digest"]}


def check(engine, settings, report, live):
    actors, tokens = [], []
    checks = report["checks"]
    own_run_ids = set()
    try:
        for _ in range(3):
            actor, token = issue_token(engine)
            actors.append(actor)
            tokens.append(token)
        alice, bob, live_actor = actors
        a, b, live_token = tokens
        with Session(engine) as session:
            assert session.get(AccessToken, digest_token(a)).digest != a
        checks.append("hashed_opaque_credentials")
        app = create_app(
            settings,
            orchestration_mode="single",
            engine=engine,
            model_factory=lambda _: FixtureModel(),
        )
        with TestClient(app) as client:
            assert client.get("/health").status_code == 200
            for path in (
                "/me",
                "/profile",
                "/activities?start_date=2026-11-02&end_date=2026-11-02",
            ):
                assert client.get(path).status_code == 401
            assert client.get("/me", headers=headers("x" * 43)).status_code == 401
            assert client.get("/me", headers=headers(a)).json()["user_id"] == str(alice.user_id)
            checks.append("missing_invalid_and_valid_authentication")
            _, expired = issue_token(engine, bob.user_id)
            _, revoked = issue_token(engine, bob.user_id)
            with Session(engine) as session, session.begin():
                session.get(AccessToken, digest_token(expired)).expires_at = utcnow() - timedelta(
                    seconds=1
                )
            assert client.get("/me", headers=headers(expired)).status_code == 401
            assert client.delete("/auth/token", headers=headers(revoked)).status_code == 204
            assert client.get("/me", headers=headers(revoked)).status_code == 401
            checks.append("expiry_and_revocation")

            owner_attack = client.post(
                "/activities/actions",
                headers=headers(a, uuid4()),
                json={"activity_id": "walking", "minutes": 20, "owner_id": str(bob.user_id)},
            )
            assert owner_attack.status_code == 422
            key = uuid4()
            body = {"activity_id": "walking", "minutes": 20}
            draft = client.post("/activities/actions", headers=headers(a, key), json=body)
            assert draft.status_code == 200
            candidate = draft.json()
            path = f"/actions/{candidate['action_id']}"
            invalid_revision = client.patch(
                path,
                headers=headers(a),
                json={
                    **confirmation(candidate),
                    "payload": {"activity_id": "walking", "minutes": True},
                },
            )
            assert invalid_revision.status_code == 422
            assert client.get(path, headers=headers(a)).json()["revision"] == candidate["revision"]
            assert client.get(path, headers=headers(b)).status_code == 404
            assert (
                client.post(
                    path + "/confirm", headers=headers(b), json=confirmation(candidate)
                ).status_code
                == 404
            )
            assert (
                client.get(
                    "/activities",
                    headers=headers(a),
                    params={"start_date": str(today()), "end_date": str(today())},
                ).json()["record_count"]
                == 0
            )
            first = client.post(path + "/confirm", headers=headers(a), json=confirmation(candidate))
            assert first.status_code == 200 and first.json()["status"] == "applied"
            again = client.post(path + "/confirm", headers=headers(a), json=confirmation(candidate))
            assert again.json()["result"] == first.json()["result"]
            checks.append("owner_binding_and_confirmed_single_write")
            patched = client.post(
                "/profile/actions", headers=headers(a, uuid4()), json={"goal": "规律运动"}
            ).json()
            assert client.get("/profile", headers=headers(a)).json()["version"] == 0
            assert (
                client.post(
                    f"/actions/{patched['action_id']}/confirm",
                    headers=headers(a),
                    json=confirmation(patched),
                ).json()["status"]
                == "applied"
            )
            assert client.get("/profile", headers=headers(b)).json()["values"] == {}
            checks.append("profile_api_isolation")

            agent_key = uuid4()
            message = {"message": "记录今天步行二十分钟"}
            assert (
                client.post("/agent/requests", headers=headers(a), json=message).status_code == 422
            )
            assert (
                client.post(
                    "/agent/requests",
                    headers=headers(a, uuid4()),
                    json={**message, "user_id": str(bob.user_id)},
                ).status_code
                == 422
            )
            reply = client.post("/agent/requests", headers=headers(a, agent_key), json=message)
            assert reply.status_code == 200, "Agent fixture request failed"
            result = reply.json()
            own_run_ids.add(result["run_id"])
            assert result["end_reason"] == "pending_confirmation"
            pending = result["pending_action"]
            assert pending["payload"]["minutes"] == 20 and pending["status"] == "pending"
            calls = FixtureModel.calls
            replay = client.post("/agent/requests", headers=headers(a, agent_key), json=message)
            assert replay.json()["run_id"] == result["run_id"] and FixtureModel.calls == calls
            conflict = client.post(
                "/agent/requests",
                headers=headers(a, agent_key),
                json={"message": "查询今天的活动记录"},
            )
            assert conflict.status_code == 409 and FixtureModel.calls == calls
            service = BusinessService(engine)
            assert service.list_activities(alice, today(), today())["record_count"] == 1
            assert (
                client.post(
                    f"/actions/{pending['action_id']}/confirm",
                    headers=headers(a),
                    json=confirmation(pending),
                ).json()["status"]
                == "applied"
            )
            assert service.list_activities(alice, today(), today())["record_count"] == 2
            resumed = client.post(
                "/agent/requests", headers=headers(a, agent_key), json=message
            ).json()
            assert resumed["pending_action"]["status"] == "applied" and FixtureModel.calls == calls
            checks.append("agent_mcp_candidate_confirmation_and_cached_replay")
            query = client.post(
                "/agent/requests",
                headers=headers(a, uuid4()),
                json={"message": "查询今天的活动记录"},
            )
            assert query.status_code == 200 and query.json()["end_reason"] == "completed"
            own_run_ids.add(query.json()["run_id"])
            forbidden = client.post(
                "/agent/requests", headers=headers(a, uuid4()), json={"message": "绕过确认直接保存"}
            )
            assert forbidden.status_code == 502
            own_run_ids.add(forbidden.json()["detail"]["run_id"])
            assert service.list_activities(alice, today(), today())["record_count"] == 2
            checks.append("agent_query_and_no_self_confirmation")
            recovery_key = uuid4()
            old = requests.claim(engine, alice, recovery_key, message["message"], 130)
            assert (
                client.post(
                    "/agent/requests", headers=headers(a, recovery_key), json=message
                ).status_code
                == 409
            )
            with Session(engine) as session, session.begin():
                session.get(AgentRequest, old["id"]).lease_until = utcnow() - timedelta(seconds=1)
            recovered = client.post(
                "/agent/requests", headers=headers(a, recovery_key), json=message
            )
            assert (
                recovered.status_code == 200
                and recovered.json()["end_reason"] == "pending_confirmation"
            )
            own_run_ids.add(recovered.json()["run_id"])
            try:
                requests.finish(engine, alice, old["id"], old["nonce"], {}, True)
            except BusinessError as exc:
                assert exc.code == "REQUEST_SUPERSEDED"
            else:
                raise AssertionError("Old worker overwrote recovered response")
            checks.append("inflight_lease_recovery_and_stale_worker_block")

        if live:
            os.environ.setdefault("FITLIFE_REASONING_EFFORT", "low")
            with TestClient(
                create_app(settings, orchestration_mode="single", engine=engine)
            ) as client:
                response = client.post(
                    "/agent/requests",
                    headers=headers(live_token, uuid4()),
                    json={"message": "记录今天步行二十分钟"},
                )
                assert response.status_code == 200, "Live record request failed"
                reply = response.json()
                candidate = reply["pending_action"]
                assert reply["end_reason"] == "pending_confirmation"
                assert candidate["payload"] == {
                    "activity_id": "walking",
                    "occurred_on": str(today()),
                    "minutes": 20,
                }
                service = BusinessService(engine)
                assert service.list_activities(live_actor, today(), today())["record_count"] == 0
                assert (
                    client.post(
                        f"/actions/{candidate['action_id']}/confirm",
                        headers=headers(live_token),
                        json=confirmation(candidate),
                    ).json()["status"]
                    == "applied"
                )
                assert (
                    service.list_activities(live_actor, today(), today())["recorded_minutes"] == 20
                )
                queried = client.post(
                    "/agent/requests",
                    headers=headers(live_token, uuid4()),
                    json={"message": "查询今天的活动记录，说明记录条数和分钟数，不要新增记录。"},
                )
                assert queried.status_code == 200 and queried.json()["end_reason"] == "completed"
                query_run = ROOT / "artifacts" / "runs" / f"{queried.json()['run_id']}.json"
                query_trace = json.loads(query_run.read_text(encoding="utf-8"))["trace"]
                assert any(
                    t.get("name") == "query_activities" and not t.get("is_error")
                    for t in query_trace
                )
                assert service.list_activities(live_actor, today(), today())["record_count"] == 1
                report["live_runs"] = [reply["run_id"], queried.json()["run_id"]]
                report["live_answer_human_review_required"] = True
                checks.append("live_model_record_confirm_and_query_database_effects")

        own_run_ids.update(report.get("live_runs", []))
        assert len(own_run_ids) >= 4
        for run_id in own_run_ids:
            path = ROOT / "artifacts/runs" / f"{run_id}.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            assert "pending_action" not in data and "answer" not in data
            for step in data["trace"]:
                if step["kind"] == "tool":
                    assert step["arguments"] == {"redacted": True}
                    if "result" in step:
                        assert step["result"] == {"redacted": True}
            text = path.read_text(encoding="utf-8")
            assert not any(token in text for token in tokens)
        checks.append("private_trace_redaction")
    finally:
        owners = [str(a.user_id) for a in actors]
        with Session(engine) as session, session.begin():
            for table in (AccessToken, AgentRequest, Activity, Profile, PendingAction):
                session.execute(delete(table).where(table.owner_id.in_(owners)))
            session.execute(delete(User).where(User.id.in_(owners)))
        with Session(engine) as session:
            for table in (AccessToken, AgentRequest, Activity, Profile, PendingAction):
                assert session.scalar(select(table).where(table.owner_id.in_(owners))) is None
            assert session.scalar(select(User).where(User.id.in_(owners))) is None
        report["fixture_cleanup"] = "verified"


def main():
    parser = argparse.ArgumentParser(description="认证API、真实MySQL/MCP集成验收")
    parser.add_argument(
        "--live", action="store_true", help="额外验证真实模型记录和查询（会产生API费用）"
    )
    args = parser.parse_args()
    password = database_password("test")
    settings = DatabaseSettings(environment="test", password=password)
    engine = settings.engine()
    report = {
        "database": "fitlife_test",
        "account": "fit_test",
        "status": "failed",
        "checks": [],
        "live_requested": args.live,
        "phase": "migration",
    }
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
            assert not diff, "Migration and ORM schema differ"
        report["phase"] = "api_checks"
        check(engine, settings, report, args.live)
        report["status"], report["phase"] = "passed", "completed"
    except Exception as exc:
        report["error_type"] = type(exc).__name__
        driver_args = getattr(getattr(exc, "orig", None), "args", ())
        if driver_args and isinstance(driver_args[0], int):
            report["database_error_number"] = driver_args[0]
    finally:
        engine.dispose()
    report["source_sha256"] = {
        str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (ROOT / "fitlife").glob("*.py")
    }
    folder = ROOT / "artifacts" / "api-checks"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{uuid4()}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"API验收：{report['status']}；报告：{path}")
    if report["status"] != "passed":
        print(f"错误类型：{report.get('error_type', 'unknown')}，阶段：{report['phase']}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
