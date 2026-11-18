"""Supplemental Redis timeout/recovery check, executed after the frozen load campaign."""

import asyncio
import hashlib
import json
import os
import time
from pathlib import Path
from uuid import uuid4

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fitlife.auth import issue_token
from fitlife.capacity import RedisSettings
from fitlife.database import AgentRequest, DatabaseSettings, PendingAction, User
from fitlife.evaluate import clean, state


async def verify(engine, actor, token, report):
    redis = RedisSettings().client("test")
    started = None
    headers = {"Authorization": "Bearer " + token, "Idempotency-Key": str(uuid4())}
    before = state(engine, actor)
    async with httpx.AsyncClient(base_url="http://app:8100", timeout=30, trust_env=False) as client:
        try:
            assert (await client.get("/ready")).status_code == 200
            await redis.execute_command("CLIENT", "PAUSE", 7000, "ALL")
            started = time.monotonic()
            readiness = await client.get("/ready")
            assert readiness.status_code == 503
            read = await client.get("/profile", headers=headers)
            assert read.status_code == 200
            begin = time.monotonic()
            agent = await client.post(
                "/agent/requests", headers=headers, json={"message": "查询今天的活动记录"}
            )
            report["agent_rejection_ms"] = round((time.monotonic() - begin) * 1000, 3)
            assert agent.status_code == 503
            assert agent.json()["detail"] == "CAPACITY_SERVICE_UNAVAILABLE"
            assert state(engine, actor) == before
            with Session(engine) as session:
                row = session.scalar(
                    select(AgentRequest).where(AgentRequest.owner_id == str(actor.user_id))
                )
                assert row.status == "failed"
                assert (
                    session.scalar(
                        select(func.count(PendingAction.id)).where(
                            PendingAction.owner_id == str(actor.user_id)
                        )
                    )
                    == 0
                )
            report["during_fault"] = {
                "ready_http": 503,
                "database_query_http": 200,
                "agent_http": 503,
                "request_persisted_failed": True,
                "pending_actions": 0,
                "business_state_unchanged": True,
            }
        finally:
            if started is not None:
                await asyncio.sleep(max(0, 8 - (time.monotonic() - started)))
            await redis.aclose()
        assert (await client.get("/ready")).status_code == 200
        recovered = await client.post(
            "/agent/requests", headers=headers, json={"message": "查询今天的活动记录"}
        )
        assert recovered.status_code == 200 and recovered.json()["end_reason"] == "completed"
        assert state(engine, actor) == before
        replay = await client.post(
            "/agent/requests", headers=headers, json={"message": "查询今天的活动记录"}
        )
        assert replay.status_code == 200
        assert replay.json()["run_id"] == recovered.json()["run_id"]
        with Session(engine) as session:
            rows = session.scalars(
                select(AgentRequest).where(AgentRequest.owner_id == str(actor.user_id))
            ).all()
            assert len(rows) == 1 and rows[0].status == "completed"
        report["after_fault"] = {
            "ready_http": 200,
            "agent_http": 200,
            "business_state_unchanged": True,
            "same_request_key_recovered": True,
            "cached_replay_same_run": True,
            "request_rows": 1,
        }


def main():
    if os.getenv("FITLIFE_BENCHMARK_ONLY") != "1":
        raise SystemExit("DEDICATED_BENCHMARK_ONLY")
    engine = DatabaseSettings(environment="test").engine()
    actors = []
    report = {
        "status": "failed",
        "fault": "Redis CLIENT PAUSE ALL 7000ms on dedicated isolated instance",
        "external_model_calls": 0,
        "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    path = Path("artifacts") / ("fault-" + str(uuid4()) + ".json")
    try:
        with Session(engine) as session:
            assert session.scalar(select(func.count(User.id))) == 0
        actor, token = issue_token(engine)
        actors.append(actor)
        asyncio.run(verify(engine, actor, token, report))
        report["status"] = "passed"
    finally:
        clean(engine, actors)
        report["cleanup"] = "verified"
        engine.dispose()
        path.write_text(json.dumps(report, indent=2))
        print(str(path))


if __name__ == "__main__":
    main()
