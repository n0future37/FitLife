import asyncio
import threading
import time
from typing import Annotated

import httpx
import pytest
from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.responses import StreamingResponse

from fitlife.database_admission import DatabaseRequestGate


@pytest.mark.parametrize("limit,timeout", [(0, 1), (True, 1), (2, 0)])
def test_invalid_limits(limit, timeout):
    with pytest.raises(ValueError):
        DatabaseRequestGate(limit, timeout)


@pytest.mark.asyncio
async def test_queue_timeout_and_recovery():
    gate = DatabaseRequestGate(1, 0.02)
    async with gate.slot():
        with pytest.raises(HTTPException) as error:
            async with gate.slot():
                pytest.fail("slot must not be granted")
        assert error.value.status_code == 503
    async with gate.slot():
        pass


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_leak_or_add_capacity():
    gate = DatabaseRequestGate(1, 1)
    entered = asyncio.Event()

    async def waiting():
        entered.set()
        async with gate.slot():
            pytest.fail("cancelled waiter entered")

    async with gate.slot():
        task = asyncio.create_task(waiting())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    async with gate.slot():
        with pytest.raises(HTTPException):
            gate.timeout = 0.01
            async with gate.slot():
                pytest.fail("capacity must still be one")


@pytest.mark.asyncio
async def test_sync_auth_and_two_routes_share_one_bound():
    app = FastAPI()
    gate = DatabaseRequestGate(2, 2)
    router = APIRouter(dependencies=[Depends(gate, scope="function")])
    active = peak = 0
    lock = threading.Lock()

    def work():
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.01)
        with lock:
            active -= 1
        return "actor"

    Identity = Annotated[str, Depends(work)]

    @router.get("/one")
    def one(actor: Identity):
        return {"actor": work()}

    @router.get("/two")
    def two(actor: Identity):
        return {"actor": work()}

    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        results = await asyncio.gather(
            *(client.get("/one" if i % 2 else "/two") for i in range(10))
        )
    assert all(r.status_code == 200 for r in results)
    assert peak == 2 and active == 0


@pytest.mark.asyncio
async def test_async_health_and_method_rejections_bypass_busy_database_gate():
    app = FastAPI()
    gate = DatabaseRequestGate(1, 0.02)
    router = APIRouter(dependencies=[Depends(gate, scope="function")])

    def identity():
        return "actor"

    @router.get("/database")
    def database(actor: Annotated[str, Depends(identity)]):
        return {"actor": actor}

    @router.get("/agent")
    async def agent(actor: Annotated[str, Depends(identity)]):
        return {"actor": actor}

    @app.get("/health")
    def health():
        return {"status": "ok"}

    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        async with gate.slot():
            assert (await client.get("/database")).status_code == 503
            assert (await client.get("/agent")).status_code == 200
            assert (await client.get("/health")).status_code == 200
            assert (await client.post("/database")).status_code == 405


@pytest.mark.asyncio
async def test_auth_failure_and_yield_cleanup_preserve_original_lifecycle():
    app = FastAPI()
    gate = DatabaseRequestGate(1, 1)
    router = APIRouter(dependencies=[Depends(gate, scope="function")])
    events = []

    async def identity():
        owner = asyncio.current_task()
        events.append("enter")
        try:
            yield "actor"
        finally:
            assert asyncio.current_task() is owner
            events.append("exit")

    @router.get("/fail")
    def fail(actor: Annotated[str, Depends(identity)]):
        raise HTTPException(403, detail="FORBIDDEN")

    @router.get("/ok")
    def ok(actor: Annotated[str, Depends(identity)]):
        return {"actor": actor}

    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        assert (await client.get("/fail")).status_code == 403
        assert (await client.get("/ok")).status_code == 200
    assert events == ["enter", "exit", "enter", "exit"]


@pytest.mark.asyncio
async def test_slow_upload_and_streaming_response_do_not_occupy_database_capacity():
    app = FastAPI()
    gate = DatabaseRequestGate(1, 0.02)
    router = APIRouter(dependencies=[Depends(gate, scope="function")])
    body_started, body_release = asyncio.Event(), asyncio.Event()
    stream_started, stream_release = asyncio.Event(), asyncio.Event()

    @router.post("/body")
    def body(data: dict):
        return data

    @router.get("/ok")
    def ok():
        return {"ok": True}

    async def chunks():
        stream_started.set()
        await stream_release.wait()
        yield b"done"

    @router.get("/stream")
    def stream():
        return StreamingResponse(chunks())

    async def slow_body():
        body_started.set()
        await body_release.wait()
        yield b'{"value":1}'

    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        upload = asyncio.create_task(
            client.post(
                "/body",
                content=slow_body(),
                headers={"Content-Type": "application/json"},
            )
        )
        await body_started.wait()
        assert (await client.get("/ok")).status_code == 200
        body_release.set()
        assert (await upload).status_code == 200
        download = asyncio.create_task(client.get("/stream"))
        await stream_started.wait()
        assert (await client.get("/ok")).status_code == 200
        stream_release.set()
        assert (await download).status_code == 200
