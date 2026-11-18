"""Queue complete synchronous database requests before dispatching worker threads."""

import asyncio
import inspect
from contextlib import asynccontextmanager

from fastapi import HTTPException, Request


class DatabaseRequestGate:
    def __init__(self, limit: int, timeout: float):
        if isinstance(limit, bool) or limit < 1 or timeout <= 0:
            raise ValueError("INVALID_DATABASE_ADMISSION_LIMIT")
        self.limit = limit
        self.timeout = timeout
        self._slots = asyncio.Semaphore(limit)

    async def __call__(self, request: Request):
        endpoint = request.scope["route"].endpoint
        asynchronous = inspect.iscoroutinefunction(endpoint) or inspect.iscoroutinefunction(
            getattr(endpoint, "__call__", None)
        )
        if asynchronous:
            yield
        else:
            async with self.slot():
                yield

    @asynccontextmanager
    async def slot(self):
        try:
            await asyncio.wait_for(self._slots.acquire(), timeout=self.timeout)
        except TimeoutError:
            raise HTTPException(503, detail="DATABASE_REQUEST_CAPACITY_REACHED") from None
        try:
            yield
        finally:
            self._slots.release()
