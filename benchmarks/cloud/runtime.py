"""Dedicated fixed-model HTTP benchmark, never the public production entrypoint."""

import asyncio
import json
import os

from fitlife.api import create_app
from fitlife.business import today
from fitlife.capacity import RedisCapacity, RedisSettings
from fitlife.database import DatabaseSettings
from fitlife.model import Reply


class FixedQueryModel:
    async def complete(self, instructions, items, tools):
        results = [x for x in items if x.get("type") == "function_call_output"]
        if results:
            data = json.loads(results[-1]["output"])
            return Reply(
                text=f"查询到{data['record_count']}条记录。",
                response_model="cloud-benchmark-query-v1",
            )
        await asyncio.sleep(2.5)
        call = {
            "type": "function_call",
            "name": "query_activities",
            "arguments": json.dumps(
                {"start_date": str(today()), "end_date": str(today()), "limit": 10}
            ),
            "call_id": "benchmark-query",
        }
        return Reply(calls=[call], continuation=[call], response_model="cloud-benchmark-query-v1")

    async def close(self):
        pass


def create_benchmark_app():
    if os.environ.get("FITLIFE_BENCHMARK_ONLY") != "1":
        raise RuntimeError("DEDICATED_BENCHMARK_ONLY")
    settings = DatabaseSettings(environment="test")
    capacity = RedisCapacity(
        RedisSettings(namespace="cloud-benchmark-v1", capacity=2), "test", ttl=130
    )
    return create_app(
        settings,
        capacity=capacity,
        orchestration_mode="single",
        synthetic_evaluation=True,
        evaluation_auto_titles=False,
        model_factory=lambda _: FixedQueryModel(),
        media_worker_enabled=False,
    )
