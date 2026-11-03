"""Redis跨进程容量租约；业务幂等与权威事实仍在MySQL。"""

import os
from pathlib import Path
from typing import Literal
from uuid import uuid4

from dotenv import dotenv_values
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from redis.asyncio import Redis
from redis.backoff import NoBackoff
from redis.retry import Retry

ACQUIRE = """
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[1]) then return 0 end
redis.call('ZADD', KEYS[1], now + tonumber(ARGV[2]), ARGV[3])
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]) + 1)
return 1
"""


class RedisSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FITLIFE_REDIS_", extra="ignore")
    host: str = "127.0.0.1"
    port: int = 6379
    password: SecretStr | None = None
    password_file: Path | None = None
    credentials_file: Path | None = None
    capacity: int = Field(default=2, ge=1, le=16)
    namespace: str = Field(default="agent-capacity", pattern=r"^[a-z0-9-]{1,60}$")

    def client(self, environment: Literal["dev", "test"]):
        secret = self.password
        if secret is None and self.password_file:
            secret = SecretStr(self.password_file.read_text(encoding="utf-8").strip())
        if secret is None and self.credentials_file:
            values = dotenv_values(self.credentials_file)
            value = values.get("REDIS_PASSWORD")
            secret = SecretStr(value) if value else None
        if secret is None and os.getenv("REDIS_PASSWORD"):
            secret = SecretStr(os.environ["REDIS_PASSWORD"])
        return Redis(
            host=self.host,
            port=self.port,
            db=2 if environment == "dev" else 3,
            password=secret.get_secret_value() if secret else None,
            socket_connect_timeout=2,
            socket_timeout=2,
            max_connections=8,
            retry=Retry(NoBackoff(), 0),
            decode_responses=True,
        )


class RedisCapacity:
    def __init__(self, settings, environment, *, ttl):
        if environment not in {"dev", "test"}:
            raise ValueError("INVALID_ENVIRONMENT")
        self.client = settings.client(environment)
        self.key = f"fitlife:{environment}:{settings.namespace}"
        self.capacity, self.ttl = settings.capacity, int(ttl) + 1

    async def acquire(self):
        token = str(uuid4())
        acquired = await self.client.eval(ACQUIRE, 1, self.key, self.capacity, self.ttl, token)
        return token if acquired else None

    async def release(self, token):
        await self.client.zrem(self.key, token)

    async def health(self):
        return await self.client.ping()

    async def close(self):
        await self.client.aclose()
