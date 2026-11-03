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
    api_family: Literal["responses", "chat"] = "responses"
    model: str = Field(min_length=1, max_length=150)
    key_env: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    key_file: Path | None = None
    capabilities: frozenset[Literal["text", "tools", "images"]] = frozenset({"text", "tools"})
    reasoning_effort: str | None = None

    @model_validator(mode="after")
    def safe_endpoint(self):
        url = urlsplit(self.base_url)
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
        return self


def profiles(settings):
    common = dict(
        provider=settings.provider,
        base_url=settings.base_url,
        key_env=settings.key_env,
        key_file=settings.key_file,
        reasoning_effort=settings.reasoning_effort,
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
        }
    )
