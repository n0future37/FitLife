from types import SimpleNamespace

import pytest

from fitlife.evaluation_budget import PaidBudget
from fitlife.execution import ExecutionError
from fitlife.model import ChatModel, Settings
from fitlife.model_profiles import ModelProfile


def test_profile_rejects_literal_keys_and_credential_urls():
    base = {
        "name": "local",
        "provider": "local",
        "base_url": "http://127.0.0.1:8000/v1",
        "model": "my-local-model",
        "key_env": "LOCAL_MODEL_KEY",
        "api_family": "chat",
    }
    assert ModelProfile.model_validate(base).api_family == "chat"
    with pytest.raises(ValueError):
        ModelProfile.model_validate({**base, "api_key": "not-a-real-key"})
    with pytest.raises(ValueError):
        ModelProfile.model_validate({**base, "base_url": "https://user:password@example.com/v1"})


@pytest.mark.asyncio
async def test_chat_adapter_preserves_tool_ids_outputs_and_image_parts():
    captured = []

    class Calls:
        async def create(self, **kwargs):
            captured.append(kwargs)
            msg = SimpleNamespace(content="完成", tool_calls=[])
            return SimpleNamespace(
                choices=[SimpleNamespace(finish_reason="stop", message=msg)],
                model="fixture-chat",
                usage=None,
            )

    model = object.__new__(ChatModel)
    model.settings = Settings(api_family="chat")
    model.client = SimpleNamespace(chat=SimpleNamespace(completions=Calls()))
    items = [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "看图"},
                {"type": "input_image", "image_url": "data:image/png;base64,synthetic"},
            ],
        },
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}
            ],
        },
        {"type": "function_call_output", "call_id": "c1", "output": '{"ok":true}'},
    ]
    reply = await model.complete("规则", items, [])
    assert reply.text == "完成"
    assert captured[0]["messages"][-1] == {
        "role": "tool",
        "tool_call_id": "c1",
        "content": '{"ok":true}',
    }
    assert captured[0]["messages"][1]["content"][1]["type"] == "image_url"


def test_paid_budget_is_persisted_and_blocks_before_another_call(tmp_path):
    path = tmp_path / "budget.json"
    budget = PaidBudget(path, limit="0.1")
    settings = Settings(max_output_tokens=128)
    index = budget.reserve(settings, "", [], [])
    budget.usage(index, {"input_tokens": 10, "output_tokens": 2})
    restored = PaidBudget(path, limit="0.1")
    assert len(restored.state["attempts"]) == 1 and restored.state["actual_billed_usd"] is None
    with pytest.raises(ExecutionError, match="PAID_EVALUATION_BUDGET_EXHAUSTED"):
        restored.reserve(settings, "x" * 10000, [], [])


def test_budget_migration_keeps_unknown_reserves_and_settles_known_usage_idempotently(tmp_path):
    import json
    from decimal import Decimal

    path = tmp_path / "budget.json"
    state = {
        "limit_usd": "0.3",
        "reserved_usd": "0.2",
        "attempts": [
            {
                "model": "gpt-5.6-sol",
                "reserved_usd": "0.1",
                "usage": {"input_tokens": 10, "output_tokens": 2},
                "maximum_group_rate_estimate_usd": "0.000095",
            },
            {
                "model": "gpt-5.6-sol",
                "reserved_usd": "0.1",
                "usage": None,
                "maximum_group_rate_estimate_usd": None,
            },
        ],
    }
    path.write_text(json.dumps(state), encoding="utf-8")
    budget = PaidBudget(path, limit="0.3")
    assert path.with_name("budget.policy-v1.json").is_file()
    assert budget.state["limit_usd"] == "0.3" and len(budget.state["attempts"]) == 2
    assert Decimal(budget.state["reserved_usd"]) >= Decimal("0.1")
    first = budget.state["reserved_usd"]
    budget.usage(0, {"input_tokens": 10, "output_tokens": 2})
    assert budget.state["reserved_usd"] == first
    assert budget.state["attempts"][1]["usage"] is None


def test_user_bill_checkpoint_preserves_history_limit_and_unknown_reserves(tmp_path):
    from decimal import Decimal

    from fitlife.evaluation_budget import PaidBudget
    from fitlife.execution import ExecutionError
    from fitlife.model import Settings

    budget = PaidBudget(tmp_path / "budget.json", "0.3")
    settings = Settings(max_output_tokens=128)
    first = budget.reserve(settings, "small", [], [])
    with pytest.raises(ValueError, match="INCOMPLETE_ATTEMPTS_REQUIRE_REVIEW"):
        budget.acknowledge_bill("0.01", source="user_reported", evidence="test")
    budget.usage(first, {"input_tokens": 10, "output_tokens": 2})
    previous = list(budget.state["attempts"])
    budget.acknowledge_bill("0.01", source="user_reported", evidence="user statement")
    assert budget.state["attempts"] == previous and budget.state["limit_usd"] == "0.3"
    assert budget.effective_used() == Decimal("0.01")
    budget.reserve(settings, "small", [], [])
    assert budget.effective_used() > Decimal("0.01")
    restored = PaidBudget(budget.path, "0.3")
    assert restored.effective_used() == budget.effective_used()
    with pytest.raises(ExecutionError, match="PAID_EVALUATION_BUDGET_EXHAUSTED"):
        restored.reserve(settings, "x" * 20000, [], [])
