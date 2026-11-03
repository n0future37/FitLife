"""容量机制测量：固定2.5秒模型替身，不用于声称真实模型延迟或线上吞吐。"""

import asyncio
import hashlib
import json
import platform
import statistics
import time
from pathlib import Path
from uuid import uuid4

import httpx

from fitlife.api import create_app
from fitlife.api_check import headers
from fitlife.auth import issue_token
from fitlife.capacity import RedisCapacity, RedisSettings
from fitlife.database import DatabaseSettings
from fitlife.evaluate import clean, state
from fitlife.manage import migrate
from fitlife.model import Reply

ROOT = Path(__file__).resolve().parents[1]


async def measure(settings, engine, actors, tokens):
    active = peak = calls = 0

    class DelayedModel:
        async def complete(self, *args):
            nonlocal active, peak, calls
            active += 1
            calls += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(2.5)
                return Reply(
                    text="合成容量请求完成，未修改业务数据。", response_model="delay-fixture-v1"
                )
            finally:
                active -= 1

        async def close(self):
            pass

    capacity = RedisCapacity(RedisSettings(namespace="check-" + uuid4().hex), "test", ttl=130)
    app = create_app(
        settings,
        orchestration_mode="single",
        engine=engine,
        capacity=capacity,
        model_factory=lambda _: DelayedModel(),
    )
    observations, batches = [], []
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            for repeat in range(3):

                async def send(index):
                    started = time.perf_counter()
                    response = await client.post(
                        "/agent/requests",
                        headers=headers(tokens[index], uuid4()),
                        json={"message": "仅用于合成容量流程检查"},
                    )
                    return {
                        "repeat": repeat + 1,
                        "http": response.status_code,
                        "seconds": round(time.perf_counter() - started, 3),
                    }

                started = time.perf_counter()
                rows = await asyncio.gather(*(send(i) for i in range(4)))
                batches.append(round(time.perf_counter() - started, 3))
                assert sorted(r["http"] for r in rows) == [200, 200, 429, 429]
                observations.extend(rows)
        assert peak == 2 and calls == 6
        for actor in actors:
            assert state(engine, actor) == {
                "activities": [],
                "profile": {},
                "plans": 0,
                "planned_minutes": 0,
            }
        return {
            "status": "passed",
            "samples": observations,
            "batch_seconds": batches,
            "peak_active_model_tasks": peak,
            "model_calls": calls,
            "mean_request_seconds": statistics.mean(r["seconds"] for r in observations),
            "min_request_seconds": min(r["seconds"] for r in observations),
            "max_request_seconds": max(r["seconds"] for r in observations),
        }
    finally:
        await capacity.client.delete(capacity.key)
        await capacity.close()


def main():
    settings = DatabaseSettings(environment="test")
    engine = settings.engine()
    actors, tokens = [], []
    report = {"status": "failed"}
    try:
        migrate(engine, "test")
        for _ in range(4):
            actor, token = issue_token(engine)
            actors.append(actor)
            tokens.append(token)
        report = asyncio.run(measure(settings, engine, actors, tokens))
    except Exception as exc:
        report["error_type"] = type(exc).__name__
    finally:
        clean(engine, actors)
        engine.dispose()
        report["cleanup"] = "verified"
    report["conditions"] = {
        "os": platform.platform(),
        "python": platform.python_version(),
        "mode": "fixed-model-delay-2.5s",
        "external_model_calls": 0,
        "http_concurrency": 4,
        "batches": 3,
        "n_requests": 12,
        "app_processes": 1,
        "local_capacity": 2,
        "redis_shared_capacity": 2,
        "local_queue_timeout_seconds": 2,
        "scope": (
            "ASGI API end-to-end including auth, MySQL, MCP startup, queue and fixture delay; "
            "excludes setup/cleanup"
        ),
        "limitation": "Mechanism test only; not real LLM performance or production throughput.",
    }
    report["source_sha256"] = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT / "fitlife").glob("*.py")
    }
    path = ROOT / "artifacts/performance" / f"{uuid4()}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"容量机制检查：{report['status']}；{path}")
    if report["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
