"""Responses/Chat模型适配；平台扩展参数显式配置，不猜测兼容性。"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from openai import AsyncOpenAI
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from fitlife.chat_messages import chat_messages


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FITLIFE_", extra="ignore")
    provider: str = "modelflare"
    base_url: str = "https://modelflare.dev/v1"
    model: str = "gpt-5.6-sol"
    key_env: str = "MODELFLARE_API_KEY"
    key_file: Path | None = None
    tool_policy: Literal["baseline", "direct_recording"] = "baseline"
    orchestration: Literal["single", "unified", "master", "master_serial", "master_independent"] = (
        "master"
    )
    routing_policy: Literal["fixed", "conservative", "rules"] = "conservative"
    routing_rules_file: Path | None = None
    model_profiles_file: Path | None = None
    api_family: Literal["responses", "chat", "ollama"] = "responses"
    free_only: bool = False
    model_capabilities: frozenset[str] = frozenset({"text", "tools", "images"})
    router_model: str | None = "fast"
    planner_model: str | None = None
    synthesis_model: str | None = None
    easy_model: str | None = "fast"
    vision_model: str = "vision"
    master_max_steps: int = Field(default=16, ge=1, le=24)
    master_max_model_calls: int = Field(default=12, ge=1, le=24)
    master_max_tool_calls: int = Field(default=12, ge=1, le=24)
    master_run_timeout: float = Field(default=240, gt=0, le=600)
    master_token_units: int = Field(default=350000, ge=1000, le=1000000)
    reasoning_effort: str | None = None
    enable_thinking: bool | None = None
    thinking_budget: int | None = Field(default=None, ge=128, le=32768)
    temperature: float | None = Field(default=None, ge=0, le=2)
    fallback_profile: str | None = "local"
    circuit_enabled: bool = True
    circuit_failure_threshold: int = Field(default=2, ge=1, le=20)
    circuit_cooldown: float = Field(default=30, gt=0, le=600)
    request_timeout: float = Field(default=45, gt=0, le=120)
    run_timeout: float = Field(default=100, gt=0, le=300)
    max_steps: int = Field(default=4, ge=1, le=8)
    max_tool_calls: int = Field(default=4, ge=1, le=8)
    max_output_tokens: int = Field(default=1200, ge=128, le=4096)
    max_model_calls: int = Field(default=6, ge=1, le=12)
    model_retries: int = Field(default=1, ge=0, le=2)
    readonly_tool_retries: int = Field(default=1, ge=0, le=2)
    tool_timeout: float = Field(default=10, gt=0, le=30)
    max_input_bytes: int = Field(default=60000, ge=1000, le=200000)
    max_total_token_units: int = Field(default=128000, ge=1000, le=400000)
    readonly_concurrency: int = Field(default=2, ge=1, le=4)


@dataclass
class Reply:
    text: str = ""
    calls: list[dict] = field(default_factory=list)
    continuation: list[dict] = field(default_factory=list)
    response_model: str | None = None
    usage: dict | None = None
    request_id: str | None = None


def free_request_options(settings):
    if (
        settings.provider != "openrouter"
        or settings.api_family != "chat"
        or settings.base_url.rstrip("/") != "https://openrouter.ai/api/v1"
        or not settings.model.endswith(":free")
    ):
        raise ValueError("FREE_ONLY_REQUIRES_OPENROUTER_FREE_MODEL")
    return {
        "extra_body": {
            "provider": {
                "max_price": {"prompt": 0, "completion": 0, "request": 0, "image": 0},
                "require_parameters": True,
            }
        }
    }


class ResponsesModel:
    network_model = True

    def __init__(self, settings: Settings):
        import logging

        if settings.free_only:
            free_request_options(settings)
        for name in ("openai", "httpx", "httpx2"):
            logging.getLogger(name).setLevel(logging.WARNING)
        key = os.getenv(settings.key_env)
        if not key and settings.key_file:
            key = settings.key_file.read_text(encoding="utf-8").strip()
        if not key:
            raise ValueError(f"MISSING_KEY: 请在本机设置环境变量 {settings.key_env}")
        self.settings = settings
        self.client = AsyncOpenAI(
            api_key=key,
            base_url=settings.base_url,
            timeout=settings.request_timeout,
            max_retries=0,
        )

    async def complete(self, instructions: str, items: list, tools: list) -> Reply:
        if tools and "tools" not in self.settings.model_capabilities:
            raise ValueError("MODEL_TOOLS_NOT_SUPPORTED")
        options = {}
        if self.settings.temperature is not None:
            options["temperature"] = self.settings.temperature
        if self.settings.reasoning_effort:
            options["reasoning"] = {"effort": self.settings.reasoning_effort}
        response = await self.client.responses.create(
            model=self.settings.model,
            instructions=instructions,
            input=items,
            tools=tools,
            max_output_tokens=self.settings.max_output_tokens,
            store=False,
            **options,
        )
        if response.status != "completed":
            raise RuntimeError("MODEL_INCOMPLETE")
        return Reply(
            text=response.output_text,
            calls=[
                {"name": item.name, "arguments": item.arguments, "call_id": item.call_id}
                for item in response.output
                if item.type == "function_call"
            ],
            continuation=[item.model_dump(exclude_none=True) for item in response.output],
            response_model=response.model,
            usage=response.usage.model_dump() if response.usage else None,
            request_id=getattr(response, "_request_id", None) or getattr(response, "id", None),
        )

    async def close(self):
        await self.client.close()


class ScriptedModel:
    """离线运行检查使用固定响应；不能用来报告真实模型能力。"""

    async def complete(self, instructions: str, items: list, tools: list) -> Reply:
        results = [item for item in items if item.get("type") == "function_call_output"]
        if not results:
            call = {
                "type": "function_call",
                "name": "get_activity_catalog",
                "arguments": '{"category":"all"}',
                "call_id": "offline-call-1",
            }
            return Reply(calls=[call], continuation=[call], response_model="scripted-v1")
        import json

        catalog = json.loads(results[-1]["output"])
        names = "、".join(item["name"] for item in catalog["activities"])
        return Reply(text=f"当前支持：{names}。{catalog['notice']}", response_model="scripted-v1")

    async def close(self):
        pass


class ChatModel(ResponsesModel):
    """OpenAI-compatible Chat Completions adapter; no protocol auto-detection."""

    async def complete(self, instructions, items, tools):
        if tools and "tools" not in self.settings.model_capabilities:
            raise ValueError("MODEL_TOOLS_NOT_SUPPORTED")
        messages = chat_messages(instructions, items)
        options = {}
        if self.settings.temperature is not None:
            options["temperature"] = self.settings.temperature
        if self.settings.reasoning_effort:
            options["reasoning_effort"] = self.settings.reasoning_effort
        if self.settings.free_only:
            options.update(free_request_options(self.settings))
        if self.settings.provider == "siliconflow":
            options["max_tokens"] = self.settings.max_output_tokens
            extra = {}
            if self.settings.enable_thinking is not None:
                extra["enable_thinking"] = self.settings.enable_thinking
            if self.settings.thinking_budget is not None:
                if self.settings.enable_thinking is not True:
                    raise ValueError("THINKING_BUDGET_REQUIRES_ENABLED_THINKING")
                extra["thinking_budget"] = self.settings.thinking_budget
            if extra:
                options["extra_body"] = extra
        else:
            options["max_completion_tokens"] = self.settings.max_output_tokens
        response = await self.client.chat.completions.create(
            model=self.settings.model,
            messages=messages,
            tools=[
                {"type": "function", "function": {k: v for k, v in tool.items() if k != "type"}}
                for tool in tools
            ]
            or None,
            **options,
        )
        choice = response.choices[0]
        if choice.finish_reason not in {"stop", "tool_calls"}:
            raise RuntimeError("MODEL_INCOMPLETE")
        message = choice.message
        calls = [
            {"name": c.function.name, "arguments": c.function.arguments, "call_id": c.id}
            for c in message.tool_calls or []
        ]
        continuation = {"role": "assistant", "content": message.content}
        if message.tool_calls:
            continuation["tool_calls"] = [
                c.model_dump(exclude_none=True) for c in message.tool_calls
            ]
        if getattr(message, "reasoning_details", None):
            continuation["reasoning_details"] = message.reasoning_details
        return Reply(
            text=message.content or "",
            calls=calls,
            continuation=[continuation],
            response_model=response.model,
            usage=(
                {
                    **response.usage.model_dump(),
                    "input_tokens": response.usage.prompt_tokens,
                    "output_tokens": response.usage.completion_tokens,
                }
                if response.usage
                else None
            ),
            request_id=getattr(response, "_request_id", None) or getattr(response, "id", None),
        )


def create_model(settings):
    if settings.api_family == "ollama":
        from fitlife.ollama_model import OllamaModel

        return OllamaModel(settings)
    return ChatModel(settings) if settings.api_family == "chat" else ResponsesModel(settings)
