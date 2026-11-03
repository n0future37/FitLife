"""多轮、记忆及中断恢复集中验收；仅测试库的随机用户。"""

import argparse
import hashlib
import json
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from fitlife import requests
from fitlife.api import create_app
from fitlife.api_check import headers
from fitlife.auth import issue_token
from fitlife.business import BusinessError, utcnow
from fitlife.database import (
    AccessToken,
    Activity,
    AgentRequest,
    Base,
    Conversation,
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
from fitlife.planning import PlanningService

ROOT = Path(__file__).resolve().parents[1]


class DialogueFixture:
    def __init__(self):
        self.calls = 0
        self.history_seen = False

    async def complete(self, instructions, items, tools):
        self.calls += 1
        if items[-1].get("content") == "记录今天步行":
            return Reply(text="步行了多少分钟？", response_model="dialogue-fixture-v1")
        assert items[-1]["content"] in {"二十分钟", "改成十五分钟"}
        self.history_seen = any(item.get("content") == "记录今天步行" for item in items[:-1])
        assert self.history_seen
        minutes = 15 if items[-1]["content"] == "改成十五分钟" else 20
        call = {
            "type": "function_call",
            "name": "propose_activity",
            "call_id": "fixture",
            "arguments": json.dumps({"data": {"activity_id": "walking", "minutes": minutes}}),
        }
        return Reply(calls=[call], continuation=[call], response_model="dialogue-fixture-v1")

    async def close(self):
        pass


def apply(service, actor, candidate):
    return service.confirm(
        actor, candidate["action_id"], candidate["revision"], candidate["digest"]
    )


def verify(engine, settings, report, live):
    actors = []
    service = PlanningService(engine)
    try:
        alice, token = issue_token(engine)
        actors.append(alice)
        bob, other_token = issue_token(engine)
        actors.append(bob)
        models = []

        def factory(_):
            model = DialogueFixture()
            models.append(model)
            return model

        with TestClient(
            create_app(settings, orchestration_mode="single", engine=engine, model_factory=factory)
        ) as client:
            cid = client.post("/conversations", headers=headers(token)).json()["conversation_id"]
            assert (
                client.get(f"/conversations/{cid}", headers=headers(other_token)).status_code == 404
            )
            assert (
                client.delete(f"/conversations/{cid}", headers=headers(other_token)).status_code
                == 404
            )

            def send(message, key=None):
                response = client.post(
                    "/agent/requests",
                    headers=headers(token, key or uuid4()),
                    json={"message": message, "conversation_id": cid},
                )
                assert response.status_code == 200
                return response.json()

            first = send("记录今天步行")
            assert first["end_reason"] == "completed"
            request_key = uuid4()
            second = send("二十分钟", request_key)
            old = second["pending_action"]
            assert old["payload"]["minutes"] == 20 and len(models) == 2
            send("二十分钟", request_key)
            assert len(models) == 2  # 缓存重放没有模型成本。
            third = send("改成十五分钟")
            assert third["pending_action"]["payload"]["minutes"] == 15
            assert service.get_action(alice, old["action_id"])["status"] == "superseded"
            expect_error("ACTION_NOT_PENDING", lambda: apply(service, alice, old))
            apply(service, alice, third["pending_action"])
            turns = client.get(f"/conversations/{cid}", headers=headers(token)).json()["turns"]
            assert (
                len(turns) == 3 and turns[-1]["response"]["pending_action"]["status"] == "applied"
            )
            assert models[-1].history_seen
            report["checks"].append(
                "dialogue_completion_latest_override_cached_replay_and_isolation"
            )

            profile = service.propose(
                alice, "profile_patch", {"goal": "合成目标", "session_minutes": 20}, "p"
            )
            apply(service, alice, profile)
            forget = client.post(
                "/profile/forget-actions",
                headers=headers(token, uuid4()),
                json={"fields": ["goal"]},
            ).json()
            assert service.get_profile(alice)["values"]["goal"] == "合成目标"
            apply(service, alice, forget)
            current = service.get_profile(alice)
            assert "goal" not in current["values"] and current["values"]["session_minutes"] == 20
            assert current["provenance"]["goal"]["source"] == "user_deleted"
            claim = requests.claim(engine, alice, uuid4(), "新请求", 60, cid)
            assert claim["history"] == []  # 画像版本变化，不重送旧聊天。
            requests.finish(
                engine, alice, claim["id"], claim["nonce"], {"answer": "未设置目标"}, True
            )
            report["checks"].append(
                "memory_forget_confirmation_provenance_and_old_context_exclusion"
            )

            active_key = uuid4()
            active = requests.claim(engine, alice, active_key, "恢复用请求", 60, cid)
            expect_error(
                "CONVERSATION_IN_PROGRESS",
                lambda: requests.claim(engine, alice, uuid4(), "并发新请求", 60, cid),
            )
            assert client.delete(f"/conversations/{cid}", headers=headers(token)).status_code == 409
            fenced = PlanningService(engine, request_fence=(str(active_key), active["nonce"]))
            pending = fenced.propose(
                alice,
                "record_activity",
                {"activity_id": "walking", "minutes": 7},
                "agent:" + str(active_key),
            )
            with Session(engine) as session, session.begin():
                row = session.get(AgentRequest, active["id"])
                row.lease_until = utcnow() - timedelta(seconds=1)
                conversation = session.get(Conversation, cid)
                conversation.lease_until = row.lease_until
            reclaimed = requests.claim(engine, alice, active_key, "恢复用请求", 60, cid)
            assert reclaimed["nonce"] != active["nonce"]
            expect_error(
                "REQUEST_SUPERSEDED",
                lambda: fenced.propose(
                    alice,
                    "record_activity",
                    {"activity_id": "walking", "minutes": 7},
                    "agent:" + str(active_key),
                ),
            )
            expect_error(
                "REQUEST_SUPERSEDED",
                lambda: requests.finish(
                    engine, alice, active["id"], active["nonce"], {"answer": "过期执行者"}, True
                ),
            )
            requests.finish(engine, alice, reclaimed["id"], reclaimed["nonce"], {}, False)
            before = len(models)
            recovered = send("恢复用请求", active_key)
            assert recovered["recovered"] and len(models) == before
            assert recovered["pending_action"]["action_id"] == pending["action_id"]
            apply(service, alice, recovered["pending_action"])
            report["checks"].append(
                "session_serialization_nonce_fence_and_partial_candidate_recovery"
            )

            erased = client.delete(f"/conversations/{cid}", headers=headers(token))
            assert erased.status_code == 200 and erased.json()["status"] == "erased"
            assert client.get(f"/conversations/{cid}", headers=headers(token)).status_code == 404
            assert (
                client.get("/conversations", headers=headers(token)).json()["conversations"] == []
            )
            assert len(service.list_activities(alice, "2000-01-01", "2000-01-01")["records"]) == 0
            with Session(engine) as session:
                rows = session.scalars(
                    select(AgentRequest).where(AgentRequest.conversation_id == cid)
                ).all()
                assert rows and all(
                    r.status == "erased"
                    and r.context is None
                    and r.response is None
                    and r.user_message is None
                    for r in rows
                )
                assert (
                    len(
                        session.scalars(
                            select(Activity).where(Activity.owner_id == str(alice.user_id))
                        ).all()
                    )
                    == 2
                )
            report["checks"].append(
                "erase_chat_content_and_replay_tombstones_preserve_confirmed_facts"
            )

        if live:
            with TestClient(
                create_app(settings, orchestration_mode="single", engine=engine)
            ) as client:
                cid = client.post("/conversations", headers=headers(other_token)).json()[
                    "conversation_id"
                ]
                results = []
                for message in ("记录今天步行", "二十分钟"):
                    response = client.post(
                        "/agent/requests",
                        headers=headers(other_token, uuid4()),
                        json={"message": message, "conversation_id": cid},
                    )
                    assert response.status_code == 200
                    results.append(response.json())
                assert (
                    results[0]["end_reason"] == "completed" and "pending_action" not in results[0]
                )
                candidate = results[1]["pending_action"]
                assert (
                    candidate["kind"] == "record_activity" and candidate["payload"]["minutes"] == 20
                )
                assert candidate["payload"]["activity_id"] == "walking"
                with Session(engine) as session:
                    assert (
                        session.scalar(
                            select(Activity).where(Activity.owner_id == str(bob.user_id))
                        )
                        is None
                    )
                apply(service, bob, candidate)
                report["live_runs"] = [r["run_id"] for r in results]
                report["synthetic_clarification_answer"] = results[0]["answer"]
            report["checks"].append("live_missing_minutes_clarification_then_record_candidate")
    finally:
        owners = [str(a.user_id) for a in actors]
        with Session(engine) as session, session.begin():
            plan_ids = select(Plan.id).where(Plan.owner_id.in_(owners))
            session.execute(delete(PlanRevision).where(PlanRevision.plan_id.in_(plan_ids)))
            for table in (
                AccessToken,
                AgentRequest,
                Conversation,
                Plan,
                Activity,
                Profile,
                PendingAction,
            ):
                session.execute(delete(table).where(table.owner_id.in_(owners)))
            session.execute(delete(User).where(User.id.in_(owners)))
        with Session(engine) as session:
            for table in (
                AccessToken,
                AgentRequest,
                Conversation,
                Plan,
                Activity,
                Profile,
                PendingAction,
            ):
                assert session.scalar(select(table).where(table.owner_id.in_(owners))) is None
        report["fixture_cleanup"] = "verified"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    settings = DatabaseSettings(environment="test")
    engine = settings.engine()
    report = {"status": "failed", "checks": [], "phase": "migration", "live_requested": args.live}
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
            assert not diff
        report["phase"] = "session_checks"
        verify(engine, settings, report, args.live)
        report.update(status="passed", phase="completed")
    except Exception as exc:
        report["error_type"] = type(exc).__name__
        if isinstance(exc, BusinessError):
            report["error_code"] = exc.code
        if isinstance(exc, AssertionError):
            import traceback

            report["failure_line"] = traceback.extract_tb(exc.__traceback__)[-1].lineno
    finally:
        engine.dispose()
    report["source_sha256"] = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT / "fitlife").glob("*.py")
    }
    folder = ROOT / "artifacts/session-checks"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{uuid4()}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"多轮/记忆验收：{report['status']}；报告：{path}")
    if report["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
