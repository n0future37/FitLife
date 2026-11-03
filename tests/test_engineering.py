from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import select
from sqlalchemy.orm import Session

from fitlife.api import create_app
from fitlife.api_check import FixtureModel, confirmation, headers
from fitlife.auth import issue_token
from fitlife.business import BusinessError, today
from fitlife.capacity import RedisCapacity, RedisSettings
from fitlife.database import AgentRequest
from fitlife.evaluate import clean
from fitlife.planning import PlanningService

pytestmark = pytest.mark.integration


def test_deletion_ownership_rejection_double_confirmation_and_pagination(test_database):
    settings, engine = test_database
    actors = []
    try:
        alice, token = issue_token(engine)
        actors.append(alice)
        bob, other = issue_token(engine)
        actors.append(bob)
        service = PlanningService(engine)
        for i in range(3):
            draft = service.propose(
                alice,
                "record_activity",
                {"activity_id": "walking", "minutes": i + 10},
                f"record-{i}",
            )
            service.confirm(alice, draft["action_id"], draft["revision"], draft["digest"])
        with TestClient(create_app(settings, engine=engine)) as client:
            response = client.get(
                f"/activities?start_date={today()}&end_date={today()}&limit=2",
                headers=headers(token),
            )
            data = response.json()
            assert data["record_count"] == 3 and data["recorded_minutes"] == 33
            assert len(data["records"]) == 2 and data["has_more"]
            assert (
                client.get(
                    f"/activities?start_date={today()}&end_date={today()}&limit=101",
                    headers=headers(token),
                ).status_code
                == 422
            )
            record_id = data["records"][0]["id"]
            path = f"/activities/{record_id}/deletion-actions"
            assert client.post(path, headers=headers(other, uuid4())).status_code == 404
            key = uuid4()
            pending = client.post(path, headers=headers(token, key)).json()
            assert pending["kind"] == "delete_activity"
            assert service.list_activities(alice, today(), today())["record_count"] == 3
            reject = client.post(
                f"/actions/{pending['action_id']}/reject",
                headers=headers(token),
                json=confirmation(pending),
            )
            assert reject.json()["status"] == "rejected"
            pending = client.post(path, headers=headers(token, uuid4())).json()
            with pytest.raises(BusinessError, match="OPERATION_SCOPE_CHANGE"):
                service.revise(
                    alice,
                    pending["action_id"],
                    pending["revision"],
                    pending["digest"],
                    {**pending["payload"], "record_id": str(uuid4())},
                )
            for _ in range(2):
                applied = client.post(
                    f"/actions/{pending['action_id']}/confirm",
                    headers=headers(token),
                    json=confirmation(pending),
                )
                assert applied.json()["status"] == "applied"
            assert service.list_activities(alice, today(), today())["record_count"] == 2
            assert service.get_profile(bob)["values"] == {}
    finally:
        clean(engine, actors)


@pytest.mark.asyncio
async def test_redis_global_slots_are_shared_and_released_by_exact_token():
    import asyncio

    settings = RedisSettings(namespace="check-" + uuid4().hex)
    one, two = RedisCapacity(settings, "test", ttl=30), RedisCapacity(settings, "test", ttl=30)
    try:
        tokens = await asyncio.gather(one.acquire(), two.acquire(), one.acquire(), two.acquire())
        winners = [t for t in tokens if t]
        assert len(winners) == 2 and one.key.startswith("fitlife:test:")
        assert one.client.connection_pool.connection_kwargs["db"] == 3
        await one.release(str(uuid4()))
        assert await two.acquire() is None
        await one.release(winners[0])
        assert await two.acquire() is not None
    finally:
        await one.client.delete(one.key)  # 仅本次随机命名的测试键，绝不FLUSHDB。
        await one.close()
        await two.close()


def test_redis_outage_is_closed_and_request_can_retry(test_database):
    settings, engine = test_database
    actors = []

    class Down:
        async def acquire(self):
            raise RedisConnectionError()

        async def close(self):
            pass

    try:
        actor, token = issue_token(engine)
        actors.append(actor)
        key = uuid4()
        with TestClient(create_app(settings, engine=engine, capacity=Down())) as client:
            response = client.post(
                "/agent/requests", headers=headers(token, key), json={"message": "查询目录"}
            )
            assert response.status_code == 503
        with Session(engine) as session:
            request = session.scalar(
                select(AgentRequest).where(
                    AgentRequest.owner_id == str(actor.user_id),
                    AgentRequest.request_key == str(key),
                )
            )
            assert request.status == "failed"
    finally:
        clean(engine, actors)


def test_redis_capacity_healthy_api_round_trip(test_database):
    settings, engine = test_database
    actors = []
    try:
        actor, token = issue_token(engine)
        actors.append(actor)
        capacity = RedisCapacity(RedisSettings(namespace="check-" + uuid4().hex), "test", ttl=130)
        with TestClient(
            create_app(
                settings,
                engine=engine,
                capacity=capacity,
                model_factory=lambda _: FixtureModel(),
                orchestration_mode="single",
            )
        ) as client:
            assert client.get("/ready").json() == {"status": "ready", "shared_capacity": True}
            response = client.post(
                "/agent/requests",
                headers=headers(token, uuid4()),
                json={"message": "查询今天的活动记录"},
            )
            assert response.status_code == 200 and response.json()["end_reason"] == "completed"
    finally:
        clean(engine, actors)
