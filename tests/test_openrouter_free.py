import json

import httpx
import pytest
from openai import AsyncOpenAI
from pydantic import ValidationError

from fitlife.model import ChatModel, Settings
from fitlife.model_profiles import ModelProfile, configured


def test_free_profile_rejects_paid_model_and_other_endpoints():
    values = dict(
        name="free",
        provider="openrouter",
        base_url="https://openrouter.ai/api/v1",
        api_family="chat",
        model="google/example:free",
        key_env="TEST_KEY",
        free_only=True,
    )
    profile = ModelProfile(**values)
    assert configured(Settings(), profile).free_only
    for update in (
        {"model": "google/example"},
        {"provider": "other"},
        {"base_url": "https://example.org/v1"},
        {"api_family": "responses"},
    ):
        with pytest.raises(ValidationError):
            ModelProfile(**{**values, **update})


@pytest.mark.asyncio
async def test_free_chat_sends_zero_price_cap_and_no_paid_fallback(monkeypatch):
    monkeypatch.setenv("FREE_TEST_KEY", "synthetic-not-real")
    captured = []

    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "synthetic",
                "object": "chat.completion",
                "created": 0,
                "model": "google/example:free",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "ok"},
                    }
                ],
            },
        )

    settings = Settings(
        provider="openrouter",
        base_url="https://openrouter.ai/api/v1",
        api_family="chat",
        model="google/example:free",
        key_env="FREE_TEST_KEY",
        free_only=True,
    )
    model = ChatModel(settings)
    await model.close()
    model.client = AsyncOpenAI(
        api_key="synthetic-not-real",
        base_url=settings.base_url,
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    try:
        assert (await model.complete("test", [{"role": "user", "content": "ok"}], [])).text == "ok"
        assert captured[0]["provider"]["max_price"] == {
            "prompt": 0,
            "completion": 0,
            "request": 0,
            "image": 0,
        }
        assert captured[0]["provider"]["require_parameters"] is True
        assert not captured[0].get("models") and not captured[0].get("plugins")
        model.settings = settings.model_copy(update={"model": "google/paid"})
        with pytest.raises(ValueError, match="FREE_ONLY_REQUIRES"):
            await model.complete("test", [], [])
        assert len(captured) == 1
    finally:
        await model.close()
