import json

import httpx
import pytest
from openai import AsyncOpenAI
from pydantic import ValidationError

from fitlife.circuit import endpoint_key
from fitlife.model import ChatModel, Settings
from fitlife.model_profiles import ModelProfile, configured


def test_thinking_profiles_are_explicit_validated_and_separate_circuits():
    values = dict(
        name="complex",
        provider="siliconflow",
        api_family="chat",
        base_url="https://api.siliconflow.cn/v1",
        model="Qwen/Qwen3-32B",
        key_env="SILI_API_KEY",
        enable_thinking=True,
        thinking_budget=512,
    )
    profile = ModelProfile(**values)
    settings = configured(Settings(), profile)
    assert settings.enable_thinking and settings.thinking_budget == 512
    assert endpoint_key(settings) != endpoint_key(
        settings.model_copy(update={"enable_thinking": False, "thinking_budget": None})
    )
    for patch in ({"enable_thinking": False}, {"provider": "other"}, {"thinking_budget": 127}):
        with pytest.raises(ValidationError):
            ModelProfile(**{**values, **patch})


@pytest.mark.asyncio
async def test_siliconflow_uses_own_token_and_thinking_parameters(monkeypatch):
    monkeypatch.setenv("SILI_TEST_KEY", "synthetic-key")
    captured = []

    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "synthetic",
                "object": "chat.completion",
                "created": 0,
                "model": "Qwen/Qwen3-8B",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "42"},
                    }
                ],
                "usage": {"prompt_tokens": 8, "completion_tokens": 3, "total_tokens": 11},
            },
        )

    settings = Settings(
        provider="siliconflow",
        api_family="chat",
        model="Qwen/Qwen3-8B",
        base_url="https://api.siliconflow.cn/v1",
        key_env="SILI_TEST_KEY",
        enable_thinking=True,
        thinking_budget=128,
        max_output_tokens=256,
    )
    model = ChatModel(settings)
    await model.close()
    model.client = AsyncOpenAI(
        api_key="synthetic-key",
        base_url=settings.base_url,
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    try:
        assert (await model.complete("test", [{"role": "user", "content": "6*7"}], [])).text == "42"
        body = captured[0]
        assert body["max_tokens"] == 256 and "max_completion_tokens" not in body
        assert body["enable_thinking"] is True and body["thinking_budget"] == 128
        assert "reasoning_effort" not in body and "provider" not in body
    finally:
        await model.close()


def test_cny_budget_survives_restart_and_keeps_unknown_reservations(tmp_path):
    from fitlife.siliconflow_check import CNYBudget

    prices = {
        "fixture": {
            "context_length": 100000,
            "rates_cny_per_million": {"prompt": "1", "completion": "4"},
        }
    }
    path = tmp_path / "budget.json"
    settings = Settings(
        provider="siliconflow",
        base_url="https://api.siliconflow.cn/v1",
        model="fixture",
        max_output_tokens=1000,
    )
    budget = CNYBudget(path, prices)
    for _ in range(3):
        budget.reserve(settings)
    restarted = CNYBudget(path, prices)
    assert len(restarted.state["attempts"]) == 3
    assert all(e["usage"] is None for e in restarted.state["attempts"])
    with pytest.raises(ValueError, match="BUDGET_EXHAUSTED"):
        restarted.reserve(settings)
    assert len(restarted.state["attempts"]) == 3
