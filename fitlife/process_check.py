"""真实进程退出/双实例/短并发验收，仅测试库、固定模型、随机合成用户。"""

import argparse
import asyncio
import hashlib
import json
import math
import os
import platform
import socket
import statistics
import subprocess
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import uvicorn
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from fitlife import requests
from fitlife.api import create_app
from fitlife.api_check import FixtureModel, confirmation, headers
from fitlife.auth import issue_token
from fitlife.business import today, utcnow
from fitlife.capacity import RedisCapacity, RedisSettings
from fitlife.database import Activity, AgentRequest, DatabaseSettings, PendingAction
from fitlife.evaluate import clean
from fitlife.manage import migrate
from fitlife.plan_types import PlanRequest
from fitlife.planning import PlanningService

ROOT = Path(__file__).resolve().parents[1]


def child(port, folder, label, crash):
    if not folder.resolve().is_relative_to((ROOT / "artifacts/process-checks").resolve()):
        raise ValueError("INVALID_PROBE_DIRECTORY")
    settings = DatabaseSettings(environment="test")
    engine = settings.engine()
    with engine.connect() as con:
        assert con.exec_driver_sql("SELECT DATABASE()").scalar_one() == "fitlife_test"
    lock = threading.Lock()
    stats = dict(active=0, peak=0, model_calls=0)

    @event.listens_for(engine, "checkout")
    def checkout(*_):
        with lock:
            stats["active"] += 1
            stats["peak"] = max(stats["active"], stats["peak"])

    @event.listens_for(engine, "checkin")
    def checkin(*_):
        with lock:
            stats["active"] -= 1

    class Fixed(FixtureModel):
        async def complete(self, *args):
            stats["model_calls"] += 1
            await asyncio.sleep(0.25)
            return await super().complete(*args)

    if crash:

        def terminate_after_candidate(*args):
            (folder / "crash.json").write_text(
                json.dumps(
                    dict(phase="after_candidate_commit_before_request_finish", exit_code=23)
                ),
                encoding="utf-8",
            )
            os._exit(23)

        requests.finish = terminate_after_candidate
    capacity = RedisCapacity(RedisSettings(namespace="probe-" + folder.name), "test", ttl=130)
    app = create_app(
        settings,
        engine=engine,
        model_factory=lambda _: Fixed(),
        capacity=capacity,
        orchestration_mode="single",
        synthetic_evaluation=True,
    )

    @app.get("/probe/status")
    def status():
        return stats

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="critical", access_log=False)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def run():
    folder = ROOT / "artifacts/process-checks" / uuid4().hex
    folder.mkdir(parents=True)
    settings = DatabaseSettings(environment="test")
    engine = settings.engine()
    migrate(engine, "test")
    actor, token = issue_token(engine)
    other, other_token = issue_token(engine)
    service = PlanningService(engine)
    children, logs = [], []
    report = dict(
        status="failed",
        checks={},
        real_model_calls=0,
        data_classification="synthetic",
        scope=(
            "Real processes/HTTP/MySQL/Redis/MCP; fixed model delay 0.25s/call. "
            "Single executor kernel; not model performance or production capacity."
        ),
    )
    report.update(
        started_at=datetime.now(UTC).isoformat(),
        python=platform.python_version(),
        source_sha256={
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (ROOT / "fitlife").glob("*.py")
        },
    )
    checks = report["checks"]

    async def clear_own_redis_lease():
        client = RedisSettings().client("test")
        key = "fitlife:test:probe-" + folder.name
        try:
            await client.delete(key)
            assert await client.exists(key) == 0
        finally:
            await client.aclose()

    def start(label, crash=False):
        port = free_port()
        log = (folder / (label + ".log")).open("w", encoding="utf-8")
        logs.append(log)
        args = [
            sys.executable,
            "-m",
            "fitlife.process_check",
            "--serve",
            "--port",
            str(port),
            "--folder",
            str(folder),
            "--label",
            label,
        ]
        if crash:
            args.append("--crash")
        process = subprocess.Popen(
            args, cwd=ROOT, env=os.environ.copy(), stdout=log, stderr=subprocess.STDOUT
        )
        children.append(process)
        url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline and process.poll() is None:
            try:
                if httpx.get(url + "/ready", trust_env=False, timeout=2).status_code == 200:
                    return process, url
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        raise RuntimeError("PROBE_START_FAILED")

    def post(url, path, body, key=None, credential=token):
        return httpx.post(
            url + path, json=body, headers=headers(credential, key), trust_env=False, timeout=30
        )

    try:
        crashed, crash_url = start("crash", True)
        key = uuid4()
        body = dict(message="记录今天步行二十分钟")
        try:
            post(crash_url, "/agent/requests", body, key)
        except httpx.HTTPError:
            pass
        crashed.wait(timeout=15)
        checks["actual_process_exit_23"] = (
            crashed.returncode == 23 and (folder / "crash.json").is_file()
        )
        _, first = start("instance-a")
        _, second = start("instance-b")
        early = post(first, "/agent/requests", body, key)
        checks["unexpired_lease_blocks_takeover"] = early.status_code == 409
        with Session(engine) as session, session.begin():
            row = session.scalar(
                select(AgentRequest).where(
                    AgentRequest.owner_id == str(actor.user_id),
                    AgentRequest.request_key == str(key),
                )
            )
            assert row and row.status == "running"
            row.lease_until = utcnow() - timedelta(seconds=1)
        report["lease_expiry_accelerated_in_test"] = True
        asyncio.run(clear_own_redis_lease())
        report["orphan_redis_lease_removed_for_bounded_test"] = True
        recovered = post(first, "/agent/requests", body, key)
        checks["candidate_recovered"] = (
            recovered.status_code == 200 and recovered.json().get("recovered") is True
        )
        action = recovered.json()["pending_action"]
        checks["recovery_without_model_replay"] = (
            httpx.get(first + "/probe/status", trust_env=False).json()["model_calls"] == 0
        )
        foreign = post(
            second,
            f"/actions/{action['action_id']}/confirm",
            confirmation(action),
            credential=other_token,
        )
        checks["foreign_confirm_blocked"] = foreign.status_code == 404
        with ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(
                pool.map(
                    lambda url: post(
                        url, f"/actions/{action['action_id']}/confirm", confirmation(action)
                    ),
                    [first, second],
                )
            )
        checks["two_instance_same_confirmation"] = all(
            r.status_code == 200 and r.json().get("status") == "applied" for r in responses
        )
        with Session(engine) as session:
            checks["one_activity_after_crash_and_double_confirmation"] = (
                session.scalar(
                    select(func.count())
                    .select_from(Activity)
                    .where(Activity.owner_id == str(actor.user_id))
                )
                == 1
            )
        mismatch = post(second, "/agent/requests", dict(message="不同内容"), key)
        checks["same_key_different_input_blocked"] = mismatch.status_code == 409
        new_key = uuid4()
        with ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(
                pool.map(lambda url: post(url, "/agent/requests", body, new_key), [first, second])
            )
        checks["concurrent_same_request"] = all(
            r.status_code in {200, 409} for r in replies
        ) and any(r.status_code == 200 for r in replies)
        with Session(engine) as session:
            checks["single_candidate_for_concurrent_request"] = (
                session.scalar(
                    select(func.count())
                    .select_from(PendingAction)
                    .where(
                        PendingAction.owner_id == str(actor.user_id),
                        PendingAction.request_key == "agent:" + str(new_key),
                    )
                )
                == 1
            )
        profile = service.propose(
            actor,
            "profile_patch",
            dict(
                available_weekdays=[1, 5],
                weekly_sessions=2,
                session_minutes=20,
                preferred_activities=["walking"],
            ),
            str(uuid4()),
        )
        service.confirm(actor, profile["action_id"], profile["revision"], profile["digest"])
        week = today() - timedelta(days=today().weekday()) + timedelta(days=7)
        initial = service.propose_plan(
            actor, PlanRequest(week_start=week, expected_plan_version=0), str(uuid4())
        )
        service.confirm(actor, initial["action_id"], initial["revision"], initial["digest"])
        alternatives = [
            service.propose_plan(
                actor,
                PlanRequest(week_start=week, expected_plan_version=1, session_minutes=m),
                str(uuid4()),
            )
            for m in [15, 25]
        ]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda pair: post(
                        pair[0], f"/actions/{pair[1]['action_id']}/confirm", confirmation(pair[1])
                    ),
                    zip([first, second], alternatives),
                )
            )
        checks["same_plan_version_conflict"] = (
            sorted(r.json().get("status") for r in results) == ["applied", "superseded"]
            and service.get_plan(actor, week)["version"] == 2
        )
        samples = []
        for batch in range(5):

            def query(index):
                started = time.perf_counter()
                response = post(
                    [first, second][index % 2],
                    "/agent/requests",
                    dict(message="查询今天的活动记录"),
                    uuid4(),
                )
                return dict(
                    batch=batch, status=response.status_code, seconds=time.perf_counter() - started
                )

            with ThreadPoolExecutor(max_workers=4) as pool:
                samples.extend(pool.map(query, range(4)))
        counts = dict(Counter(s["status"] for s in samples))
        checks["shared_capacity_fail_closed"] = (
            set(counts) <= {200, 429} and counts.get(200, 0) > 0 and counts.get(429, 0) > 0
        )
        pools = [
            httpx.get(url + "/probe/status", trust_env=False).json() for url in [first, second]
        ]
        checks["pool_connections_returned"] = all(
            p["active"] == 0 and p["peak"] <= 5 for p in pools
        )
        times = sorted(s["seconds"] for s in samples)
        report.update(
            samples=samples,
            status_counts=counts,
            pool_observations=pools,
            latency=dict(
                n=len(times),
                p50=statistics.median(times),
                p95=times[math.ceil(len(times) * 0.95) - 1],
                min=min(times),
                max=max(times),
            ),
            status="passed" if all(checks.values()) else "failed",
        )
        report["latency_by_status"] = {}
        for code in sorted(counts):
            values = sorted(s["seconds"] for s in samples if s["status"] == code)
            report["latency_by_status"][str(code)] = dict(
                n=len(values),
                p50=statistics.median(values),
                p95=values[math.ceil(len(values) * 0.95) - 1],
            )
    except Exception as exc:
        report["error_type"] = type(exc).__name__
        if isinstance(exc, RuntimeError):
            report["error_code"] = str(exc)
    finally:
        for process in children:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=15)
        for log in logs:
            log.close()
        clean(engine, [actor, other])
        asyncio.run(clear_own_redis_lease())
        engine.dispose()
        report["own_processes_stopped"] = all(p.poll() is not None for p in children)
        report["fixture_cleanup"] = "verified"
        report["own_redis_key_cleanup"] = "verified"
        report["ended_at"] = datetime.now(UTC).isoformat()
        (folder / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(f"Process checks: {report['status']}; {folder / 'report.json'}")
    if report["status"] != "passed":
        raise SystemExit(1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--crash", action="store_true")
    parser.add_argument("--port", type=int)
    parser.add_argument("--folder", type=Path)
    parser.add_argument("--label", default="probe")
    args = parser.parse_args()
    if args.serve:
        if not args.port or not args.folder:
            parser.error("--port and --folder required")
        child(args.port, args.folder, args.label, args.crash)
    else:
        run()


if __name__ == "__main__":
    main()
