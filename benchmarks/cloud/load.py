"""Real TCP closed-loop benchmark with synthetic users and verified side effects."""

import argparse
import asyncio
import json
import math
import os
import platform
import statistics
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from fitlife.auth import issue_token
from fitlife.business import BusinessService, fingerprint, today, utcnow
from fitlife.capacity import RedisSettings
from fitlife.database import Activity, DatabaseSettings, PendingAction, User
from fitlife.evaluate import clean
from fitlife.manage import migrate


def percentile(values, fraction):
    values = sorted(values)
    return round(values[max(0, math.ceil(len(values) * fraction) - 1)], 3) if values else None


def aggregate(rows, elapsed):
    good = [r["ms"] for r in rows if r["ok"]]
    return {
        "operations": len(rows),
        "successful_operations": len(good),
        "elapsed_seconds": round(elapsed, 4),
        "successful_operations_per_second": round(len(good) / elapsed, 3),
        "all_operations_per_second": round(len(rows) / elapsed, 3),
        "errors": len(rows) - len(good),
        "p50_success_ms": percentile(good, 0.5),
        "p95_success_ms": percentile(good, 0.95),
        "p99_success_ms": percentile(good, 0.99),
        "mean_success_ms": round(statistics.mean(good), 3) if good else None,
        "http_statuses": dict(Counter(str(code) for r in rows for code in r["statuses"])),
    }


def seed(engine, actors, tokens, n_users=32, per_user=100):
    service = BusinessService(engine)
    now = utcnow()
    for _ in range(n_users):
        actor, token = issue_token(engine)
        actors.append(actor)
        tokens.append(token)
        candidate = service.propose(
            actor, "profile_patch", {"goal": "建立规律步行习惯"}, str(uuid4())
        )
        service.confirm(actor, candidate["action_id"], candidate["revision"], candidate["digest"])
        with Session(engine) as session, session.begin():
            for _ in range(per_user):
                action_id, record_id = str(uuid4()), str(uuid4())
                payload = {
                    "activity_id": "walking",
                    "occurred_on": str(today()),
                    "minutes": 20,
                }
                action = PendingAction(
                    id=action_id,
                    owner_id=str(actor.user_id),
                    request_key="seed:" + str(uuid4()),
                    kind="record_activity",
                    payload=payload,
                    revision=1,
                    digest="",
                    base_profile_version=1,
                    base_plan_version=None,
                    status="applied",
                    created_at=now,
                    expires_at=now + timedelta(days=1),
                    result={"activity_id": record_id},
                )
                action.digest = fingerprint(action)
                session.add(action)
                session.flush()
                session.add(
                    Activity(
                        id=record_id,
                        owner_id=str(actor.user_id),
                        source_action_id=action_id,
                        activity_id="walking",
                        occurred_on=today(),
                        minutes=20,
                        created_at=now,
                    )
                )


def count(engine, actors):
    with Session(engine) as session:
        return session.scalar(
            select(func.count(Activity.id)).where(
                Activity.owner_id.in_([str(a.user_id) for a in actors])
            )
        )


async def safeguards(client, tokens, engine, actors):
    headers = {"Authorization": "Bearer " + tokens[0], "Idempotency-Key": str(uuid4())}
    body = {"activity_id": "walking", "occurred_on": str(today()), "minutes": 20}
    assert (await client.get("/profile")).status_code == 401
    start = count(engine, actors)
    candidates = await asyncio.gather(
        *(client.post("/activities/actions", headers=headers, json=body) for _ in range(32))
    )
    assert all(r.status_code == 200 for r in candidates)
    assert len({r.json()["action_id"] for r in candidates}) == 1
    candidate = candidates[0].json()
    assert count(engine, actors) == start
    path = "/actions/" + candidate["action_id"] + "/confirm"
    confirmation = {"revision": candidate["revision"], "digest": candidate["digest"]}
    other = {"Authorization": "Bearer " + tokens[1]}
    assert (await client.post(path, headers=other, json=confirmation)).status_code == 404
    assert (
        await client.post(path, headers=headers, json={**confirmation, "digest": "0" * 64})
    ).status_code == 409
    assert count(engine, actors) == start
    confirmations = await asyncio.gather(
        *(client.post(path, headers=headers, json=confirmation) for _ in range(32))
    )
    assert all(r.status_code == 200 and r.json()["status"] == "applied" for r in confirmations)
    assert len({r.json()["result"]["activity_id"] for r in confirmations}) == 1
    assert count(engine, actors) == start + 1
    assert (
        await client.post("/activities/actions", headers=headers, json={**body, "minutes": 21})
    ).status_code == 409
    assert (
        await client.post(
            "/activities/actions",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={**body, "minutes": 0},
        )
    ).status_code == 422
    return {
        "unauthenticated_401": True,
        "foreign_confirmation_404": True,
        "unconfirmed_business_writes": 0,
        "invalid_digest_409": True,
        "duplicate_candidates_32": 1,
        "duplicate_confirmations_32_business_rows": 1,
        "same_key_changed_payload_409": True,
        "invalid_input_422": True,
    }


async def agent_capacity(client, tokens):
    redis = RedisSettings().client("test")
    key = "fitlife:test:cloud-benchmark-v1"
    observations, peaks = [], []
    try:
        for repeat in range(3):
            stop = asyncio.Event()
            peak = 0

            async def observe():
                nonlocal peak
                while not stop.is_set():
                    peak = max(peak, await redis.zcard(key))
                    await asyncio.sleep(0.02)

            async def send(i):
                request_key = str(uuid4())
                headers = {
                    "Authorization": "Bearer " + tokens[i],
                    "Idempotency-Key": request_key,
                }
                started = time.perf_counter()
                r = await client.post(
                    "/agent/requests",
                    headers=headers,
                    json={"message": "查询今天的活动记录"},
                )
                row = {
                    "repeat": repeat + 1,
                    "http": r.status_code,
                    "ms": round((time.perf_counter() - started) * 1000, 3),
                }
                assert r.status_code in (200, 429)
                if r.status_code == 200:
                    data = r.json()
                    assert data["end_reason"] == "completed" and data["steps"] == 2
                    replay = await client.post(
                        "/agent/requests",
                        headers=headers,
                        json={"message": "查询今天的活动记录"},
                    )
                    assert replay.status_code == 200 and replay.json()["run_id"] == data["run_id"]
                    row["cached_replay_same_run"] = True
                return row

            monitor = asyncio.create_task(observe())
            try:
                observations.extend(await asyncio.gather(*(send(i) for i in range(8))))
            finally:
                stop.set()
                await monitor
            assert peak <= 2
            assert await redis.zcard(key) == 0
            peaks.append(peak)
        assert any(r["http"] == 200 for r in observations)
        assert any(r["http"] == 429 for r in observations)
        return {
            "fixed_model_delay_seconds": 2.5,
            "requests": observations,
            "observed_peak_shared_leases": peaks,
            "remaining_leases": 0,
            "scope": "one fixed-model query through real MCP; not LLM quality or throughput",
        }
    finally:
        await redis.aclose()


async def measure(args, engine, actors, tokens, report, folder):
    limits = httpx.Limits(max_connections=64, max_keepalive_connections=64, keepalive_expiry=2)
    async with httpx.AsyncClient(
        base_url=args.base_url, timeout=30, limits=limits, trust_env=False
    ) as client:
        assert (await client.get("/ready")).status_code == 200
        report["safeguards"] = await safeguards(client, tokens, engine, actors)
        read_counts = [101] + [100] * (len(actors) - 1)
        successful_writes = 0
        first_count = count(engine, actors)
        with (folder / "requests.jsonl").open("w", encoding="utf-8") as raw:

            async def operation(scenario, index, phase):
                nonlocal successful_writes
                i = index % len(tokens)
                headers = {"Authorization": "Bearer " + tokens[i]}
                started = time.perf_counter()
                statuses = []
                row = {"scenario": scenario, "phase": phase, "ok": False}
                try:
                    if scenario == "profile":
                        r = await client.get("/profile", headers=headers)
                        statuses.append(r.status_code)
                        row["ok"] = (
                            r.status_code == 200
                            and r.json().get("version") == 1
                            and r.json()["values"].get("goal") == "建立规律步行习惯"
                        )
                    elif scenario == "activities":
                        r = await client.get(
                            "/activities",
                            headers=headers,
                            params={
                                "start_date": str(today()),
                                "end_date": str(today()),
                                "limit": 20,
                            },
                        )
                        statuses.append(r.status_code)
                        row["ok"] = (
                            r.status_code == 200
                            and r.json().get("record_count") == read_counts[i]
                            and r.json().get("returned_count") == 20
                        )
                    else:
                        headers["Idempotency-Key"] = str(uuid4())
                        r = await client.post(
                            "/activities/actions",
                            headers=headers,
                            json={
                                "activity_id": "walking",
                                "occurred_on": str(today()),
                                "minutes": 20,
                            },
                        )
                        statuses.append(r.status_code)
                        if r.status_code == 200:
                            c = r.json()
                            r = await client.post(
                                "/actions/" + c["action_id"] + "/confirm",
                                headers=headers,
                                json={"revision": c["revision"], "digest": c["digest"]},
                            )
                            statuses.append(r.status_code)
                            row["ok"] = r.status_code == 200 and r.json().get("status") == "applied"
                            successful_writes += int(row["ok"])
                except (httpx.HTTPError, ValueError, KeyError) as exc:
                    row["error_type"] = type(exc).__name__
                    cause = exc.__cause__ or exc.__context__
                    row["cause_type"] = type(cause).__name__ if cause else None
                row.update(
                    statuses=statuses,
                    ms=round((time.perf_counter() - started) * 1000, 3),
                )
                return row

            for scenario in args.scenarios:
                for concurrency in args.concurrencies:
                    for repeat in range(args.repeats):
                        warm = await asyncio.gather(
                            *(operation(scenario, i, "warmup") for i in range(concurrency))
                        )
                        for r in warm:
                            raw.write(
                                json.dumps(
                                    {
                                        **r,
                                        "concurrency": concurrency,
                                        "repeat": repeat + 1,
                                    }
                                )
                                + "\n"
                            )
                        assert all(r["ok"] for r in warm)
                        started = time.perf_counter()
                        deadline = started + args.seconds
                        rows = []

                        async def worker(index):
                            sequence = index
                            while time.perf_counter() < deadline:
                                rows.append(await operation(scenario, sequence, "measured"))
                                sequence += concurrency

                        await asyncio.gather(*(worker(i) for i in range(concurrency)))
                        elapsed = time.perf_counter() - started
                        for r in rows:
                            raw.write(
                                json.dumps(
                                    {
                                        **r,
                                        "concurrency": concurrency,
                                        "repeat": repeat + 1,
                                    }
                                )
                                + "\n"
                            )
                        raw.flush()
                        trial = {
                            "scenario": scenario,
                            "concurrency": concurrency,
                            "repeat": repeat + 1,
                            **aggregate(rows, elapsed),
                        }
                        report["trials"].append(trial)
                        print(json.dumps(trial), flush=True)
                        (folder / "report.json").write_text(
                            json.dumps(report, ensure_ascii=False, indent=2),
                            encoding="utf-8",
                        )
        actual = count(engine, actors) - first_count
        report["side_effects"] = {
            "acknowledged_activity_writes_including_warmup": successful_writes,
            "actual_new_activity_rows": actual,
        }
        assert actual == successful_writes
        before_agent = count(engine, actors)
        report["agent_capacity"] = (
            {"skipped_for_paired_performance": True}
            if args.skip_agent_capacity
            else await agent_capacity(client, tokens)
        )
        assert count(engine, actors) == before_agent
        assert all(t["errors"] == 0 for t in report["trials"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--migrate", action="store_true")
    parser.add_argument("--skip-agent-capacity", action="store_true")
    parser.add_argument(
        "--scenarios",
        nargs="+",
        choices=["profile", "activities", "propose_confirm"],
        default=["profile", "activities", "propose_confirm"],
    )
    parser.add_argument("--base-url", default="http://app:8100")
    parser.add_argument("--workers", type=int, choices=[1, 2], default=2)
    parser.add_argument("--seconds", type=float, default=20)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--concurrencies", type=int, nargs="+", default=[1, 8, 32])
    args = parser.parse_args()
    if os.environ.get("FITLIFE_BENCHMARK_ONLY") != "1":
        raise SystemExit("DEDICATED_BENCHMARK_ONLY")
    settings = DatabaseSettings(environment="test")
    engine = settings.engine()
    with engine.connect() as connection:
        account, db, version = connection.execute(
            text("SELECT CURRENT_USER(), DATABASE(), VERSION()")
        ).one()
        assert account.split("@", 1)[0] == "fit_test" and db == "fitlife_test"
    if args.migrate:
        migrate(engine, "test")
        engine.dispose()
        print("Migration verified.")
        return
    folder = Path("artifacts/results") / (
        datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + str(uuid4())
    )
    folder.mkdir(parents=True, exist_ok=False)
    actors, tokens = [], []
    report = {
        "status": "running",
        "started_at_utc": datetime.now(UTC).isoformat(),
        "trials": [],
        "conditions": {
            "python": platform.python_version(),
            "paired_case": os.environ.get("TAIL_PAIRED_CASE"),
            "paired_variant": os.environ.get("TAIL_PAIRED_VARIANT"),
            "agent_capacity_checked": not args.skip_agent_capacity,
            "platform": platform.platform(),
            "mysql": version,
            "app_workers": args.workers,
            "seconds_per_trial": args.seconds,
            "repeats": args.repeats,
            "concurrencies": args.concurrencies,
            "scenarios": args.scenarios,
            "seed_users": 32,
            "seed_activities": 3200,
            "real_model_calls": 0,
            "http": "real TCP through Docker internal network; pooled HTTP/1.1",
            "client_keepalive_expiry_seconds": 2,
            "load_model": "closed-loop, no think time, no fixed offered QPS",
            "latency": (
                "successful operation wall time, including auth/db/queue; "
                "write operation includes two HTTP requests"
            ),
            "excluded": "seed/migrate/warmup/cleanup, host public network and real cloud LLM",
            "limits": (
                "same-host load generator competes for 4-vCPU host; "
                "short repeated trials, not production capacity"
            ),
        },
    }
    try:
        with Session(engine) as session:
            assert session.scalar(select(func.count(User.id))) == 0, (
                "DEDICATED_EMPTY_TEST_DB_REQUIRED"
            )
        seed(engine, actors, tokens)
        assert count(engine, actors) == 3200
        asyncio.run(measure(args, engine, actors, tokens, report, folder))
        report["status"] = "passed"
    except Exception as exc:
        report.update(status="failed", error_type=type(exc).__name__)
        raise
    finally:
        try:
            clean(engine, actors)
            report["cleanup"] = "verified"
        finally:
            engine.dispose()
            report["finished_at_utc"] = datetime.now(UTC).isoformat()
            (folder / "report.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print("Report: " + str(folder / "report.json"), flush=True)


if __name__ == "__main__":
    main()
