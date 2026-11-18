"""Bounded read-only overload probe with drain and recovery; not a maximum-QPS claim."""

import argparse
import asyncio
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from benchmarks.cloud.load import aggregate, count, seed
from fitlife.business import today
from fitlife.database import DatabaseSettings, User
from fitlife.evaluate import clean


async def probe(args, engine, actors, tokens, report, folder):
    async with (
        httpx.AsyncClient(
            base_url="http://app:8100",
            timeout=30,
            trust_env=False,
            limits=httpx.Limits(
                max_connections=256, max_keepalive_connections=256, keepalive_expiry=2
            ),
        ) as client,
        httpx.AsyncClient(base_url="http://app:8100", timeout=5, trust_env=False) as health,
    ):
        original = count(engine, actors)
        raw = (folder / "requests.jsonl").open("w")

        async def run(concurrency, seconds, phase):
            started = time.perf_counter()
            deadline = started + seconds
            stopped = asyncio.Event()
            rows = []
            health_samples = []
            consecutive_failures = 0
            stop_reason = None

            async def watch():
                nonlocal consecutive_failures, stop_reason
                while not stopped.is_set():
                    begin = time.perf_counter()
                    try:
                        response = await health.get("/ready")
                        status = response.status_code
                    except httpx.HTTPError:
                        status = 0
                    consecutive_failures = consecutive_failures + 1 if status != 200 else 0
                    available = next(
                        int(line.split()[1])
                        for line in Path("/proc/meminfo").read_text().splitlines()
                        if line.startswith("MemAvailable:")
                    )
                    health_samples.append(
                        {
                            "http": status,
                            "ms": round((time.perf_counter() - begin) * 1000, 3),
                            "available_kib": available,
                        }
                    )
                    if consecutive_failures >= 3 or available < 300 * 1024:
                        stop_reason = (
                            "three_readiness_failures"
                            if consecutive_failures >= 3
                            else "host_available_memory_below_300MiB"
                        )
                        stopped.set()
                    await asyncio.sleep(1)

            async def worker(i):
                sequence = i
                while time.perf_counter() < deadline and not stopped.is_set():
                    started_request = time.perf_counter()
                    row = {"ok": False, "statuses": []}
                    try:
                        response = await client.get(
                            "/activities",
                            headers={"Authorization": "Bearer " + tokens[sequence % len(tokens)]},
                            params={
                                "start_date": str(today()),
                                "end_date": str(today()),
                                "limit": 20,
                            },
                        )
                        row["statuses"] = [response.status_code]
                        row["ok"] = (
                            response.status_code == 200
                            and response.json().get("record_count") == 100
                            and response.json().get("returned_count") == 20
                        )
                    except (httpx.HTTPError, ValueError) as exc:
                        row["error_type"] = type(exc).__name__
                        chain = []
                        seen = set()
                        cause = exc
                        while cause is not None and id(cause) not in seen:
                            seen.add(id(cause))
                            chain.append(
                                {
                                    "type": type(cause).__name__,
                                    "errno": getattr(cause, "errno", None),
                                }
                            )
                            cause = cause.__cause__ or cause.__context__
                        row["error_chain"] = chain
                    row["ms"] = round((time.perf_counter() - started_request) * 1000, 3)
                    rows.append(row)
                    sequence += concurrency

            monitor = asyncio.create_task(watch())
            try:
                await asyncio.gather(*(worker(i) for i in range(concurrency)))
                elapsed = time.perf_counter() - started
            finally:
                stopped.set()
                await monitor
            for row in rows:
                raw.write(json.dumps({**row, "concurrency": concurrency, "phase": phase}) + "\n")
            raw.flush()
            trial = {
                "phase": phase,
                "concurrency": concurrency,
                **aggregate(rows, elapsed),
                "health_samples": health_samples,
                "stop_reason": stop_reason,
            }
            if not stop_reason and trial["errors"] / max(1, trial["operations"]) >= 0.05:
                trial["stop_reason"] = "operation_error_rate_at_least_5_percent"
            if not trial["stop_reason"] and (trial["p95_success_ms"] or 0) > 5000:
                trial["stop_reason"] = "successful_operation_p95_over_5_seconds"
            if args.resource_file and args.resource_file.exists() and phase == "stress":
                samples = [
                    json.loads(line)
                    for line in args.resource_file.read_text().splitlines()
                    if line.strip()
                ]
                cpu = [
                    float(c["CPUPerc"].rstrip("%"))
                    for s in samples
                    if s["timestamp_epoch"] >= time.time() - elapsed - 3
                    for c in s.get("containers", [])
                    if "stress" in c["Name"]
                ]
                trial["driver_cpu_mean_percent"] = sum(cpu) / len(cpu) if cpu else None
                if not trial["stop_reason"] and cpu and sum(cpu) / len(cpu) >= 90:
                    trial["stop_reason"] = "load_generator_near_its_one_vcpu_limit"
            print(
                json.dumps({k: v for k, v in trial.items() if k != "health_samples"}),
                flush=True,
            )
            return trial

        try:
            report["baseline"] = await run(1, 10, "baseline")
            assert report["baseline"]["errors"] == 0
            for concurrency in (64, 128, 256):
                for repeat in range(2):
                    trial = await run(concurrency, 30, "stress")
                    trial["repeat"] = repeat + 1
                    report["trials"].append(trial)
                    (folder / "report.json").write_text(json.dumps(report, indent=2))
                    if trial["stop_reason"]:
                        report["ramp_stop_reason"] = trial["stop_reason"]
                        break
                if report.get("ramp_stop_reason"):
                    break
            report["recovery"] = await run(1, 15, "recovery")
            assert report["recovery"]["errors"] == 0
            assert all(s["http"] == 200 for s in report["recovery"]["health_samples"])
            assert count(engine, actors) == original
            report["business_rows_unchanged"] = True
        finally:
            raw.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--resource-file", type=Path)
    args = parser.parse_args()
    if os.environ.get("FITLIFE_BENCHMARK_ONLY") != "1":
        raise SystemExit("DEDICATED_BENCHMARK_ONLY")
    engine = DatabaseSettings(environment="test").engine()
    actors, tokens = [], []
    folder = Path("artifacts/results") / (
        datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-stress-" + str(uuid4())
    )
    folder.mkdir(parents=True, exist_ok=False)
    report = {
        "status": "running",
        "trials": [],
        "conditions": {
            "app_workers": 2,
            "offered_concurrencies": [64, 128, 256],
            "seconds_per_trial": 30,
            "repeats_per_level": 2,
            "successful_latency_ms": "nearest-rank, TCP activity page with auth/db",
            "driver_cpu_limit": 1,
            "load": "same-host closed-loop, no think time",
            "stop_thresholds": "engineering safety boundaries, not a business SLO",
            "limitation": "bounded probe; highest tested rate is not proven maximum capacity",
            "real_model_calls": 0,
            "client_keepalive_expiry_seconds": 2,
            "seed_users": 32,
            "seed_activities": 3200,
        },
    }
    try:
        with Session(engine) as session:
            assert session.scalar(select(func.count(User.id))) == 0
        seed(engine, actors, tokens)
        asyncio.run(probe(args, engine, actors, tokens, report, folder))
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
            (folder / "report.json").write_text(json.dumps(report, indent=2))
            print("Report: " + str(folder / "report.json"), flush=True)


if __name__ == "__main__":
    main()
