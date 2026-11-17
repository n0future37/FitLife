"""模型注册表：业务角色引用配置名；平台、协议、凭据和能力独立管理。"""

import json
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,39}$")
    provider: str = Field(min_length=1, max_length=100)
    base_url: str
    api_family: Literal["responses", "chat", "ollama"] = "responses"
    model: str = Field(min_length=1, max_length=150)
    key_env: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    key_file: Path | None = None
    capabilities: frozenset[Literal["text", "tools", "images"]] = frozenset({"text", "tools"})
    reasoning_effort: str | None = None
    request_timeout: float | None = Field(default=None, gt=0, le=120)
    free_only: bool = False
    enable_thinking: bool | None = None
    thinking_budget: int | None = Field(default=None, ge=128, le=32768)
    temperature: float | None = Field(default=None, ge=0, le=2)

    @model_validator(mode="after")
    def safe_endpoint(self):
        url = urlsplit(self.base_url)
        if (self.enable_thinking is not None or self.thinking_budget is not None) and (
            self.provider != "siliconflow" or self.api_family != "chat"
        ):
            raise ValueError("THINKING_EXTENSION_REQUIRES_SILICONFLOW_CHAT")
        if self.thinking_budget is not None and self.enable_thinking is not True:
            raise ValueError("THINKING_BUDGET_REQUIRES_ENABLED_THINKING")
        if self.free_only and (
            self.provider != "openrouter"
            or self.api_family != "chat"
            or self.base_url.rstrip("/") != "https://openrouter.ai/api/v1"
            or not self.model.endswith(":free")
        ):
            raise ValueError("FREE_ONLY_REQUIRES_OPENROUTER_FREE_MODEL")
        if url.username or url.password or url.query or url.fragment:
            raise ValueError("URL_MUST_NOT_CONTAIN_CREDENTIALS")
        if not url.hostname or (
            url.scheme != "https"
            and not (
                url.scheme == "http"
                and url.hostname in {"127.0.0.1", "localhost", "host.docker.internal"}
            )
        ):
            raise ValueError("HTTPS_OR_LOCAL_ENDPOINT_REQUIRED")
        if "text" not in self.capabilities:
            raise ValueError("TEXT_OUTPUT_REQUIRED")
        if self.api_family == "ollama" and (
            url.hostname not in {"127.0.0.1", "localhost", "host.docker.internal"}
            or self.reasoning_effort not in {None, "none"}
            or "images" in self.capabilities
        ):
            raise ValueError("OLLAMA_LOCAL_NON_THINKING_ONLY")
        return self


def profiles(settings):
    common = dict(
        provider=settings.provider,
        base_url=settings.base_url,
        key_env=settings.key_env,
        key_file=settings.key_file,
        reasoning_effort=settings.reasoning_effort,
        temperature=settings.temperature,
    )
    default = {
        "standard": ModelProfile(
            name="standard",
            model=settings.model,
            capabilities=frozenset({"text", "tools", "images"}),
            **common,
        ),
        "fast": ModelProfile(name="fast", model="gpt-6-luna", **common),
        "vision": ModelProfile(
            name="vision",
            model=settings.model,
            capabilities=frozenset({"text", "images"}),
            **common,
        ),
        "local": ModelProfile(
            name="local",
            provider="ollama",
            base_url="http://127.0.0.1:11435",
            api_family="ollama",
            model="qwen3:4b-instruct-2507-q4_K_M",
            key_env="LOCAL_UNUSED",
            capabilities=frozenset({"text", "tools"}),
            request_timeout=45,
        ),
    }
    if settings.model_profiles_file:
        data = json.loads(settings.model_profiles_file.read_text(encoding="utf-8"))
        if not isinstance(data, list) or not data or len(data) > 20:
            raise ValueError("INVALID_MODEL_PROFILES")
        values = [ModelProfile.model_validate(item) for item in data]
        if len({p.name for p in values}) != len(values):
            raise ValueError("DUPLICATE_MODEL_PROFILE")
        default = {p.name: p for p in values}
    return default


def resolve(settings, selector=None, *, capability="tools"):
    catalog = profiles(settings)
    selector = selector or "standard"
    if selector not in catalog:
        # Preserve the earlier explicit model-name settings without inheriting image capability.
        base = catalog.get("standard")
        if not base or not selector.startswith(("gpt-", "claude-", "gemini-", "deepseek-", "qwen")):
            raise ValueError("UNKNOWN_MODEL_PROFILE")
        profile = base.model_copy(
            update={
                "name": "legacy-explicit",
                "model": selector,
                "capabilities": frozenset({"text", "tools"}),
            }
        )
    else:
        profile = catalog[selector]
    if capability not in profile.capabilities:
        raise ValueError("MODEL_CAPABILITY_NOT_SUPPORTED")
    return profile


def configured(settings, profile):
    return settings.model_copy(
        update={
            "provider": profile.provider,
            "base_url": profile.base_url,
            "model": profile.model,
            "key_env": profile.key_env,
            "key_file": profile.key_file,
            "api_family": profile.api_family,
            "model_capabilities": profile.capabilities,
            "reasoning_effort": profile.reasoning_effort,
            "request_timeout": profile.request_timeout or settings.request_timeout,
            "free_only": profile.free_only,
            "enable_thinking": profile.enable_thinking,
            "thinking_budget": profile.thinking_budget,
            "temperature": profile.temperature,
        }
    )
