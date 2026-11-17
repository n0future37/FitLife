import asyncio
import json

import httpx
import pytest

from fitlife.chat_messages import chat_messages
from fitlife.circuit import CircuitOpen, Circuits
from fitlife.execution import ExecutionError, Ledger, complete
from fitlife.model import Reply, Settings
from fitlife.ollama_model import OllamaModel
from fitlife.routing import Plan, Requirement, select_model


def test_circuit_half_open_single_probe_and_late_success_fencing():
    now = [0.0]
    circuits = Circuits(clock=lambda: now[0])
    first, late = circuits.acquire("a"), circuits.acquire("a")
    circuits.failure(first, 1, 10)
    circuits.success(late)
    with pytest.raises(CircuitOpen):
        circuits.acquire("a")
    now[0] = 11
    probe = circuits.acquire("a")
    with pytest.raises(CircuitOpen):
        circuits.acquire("a")
    circuits.success(probe)
    assert not circuits.acquire("a")[2]


def test_cancelled_probe_cannot_leave_circuit_permanently_locked():
    now = [1.0]
    circuits = Circuits(clock=lambda: now[0])
    circuits.failure(circuits.acquire("a"), 1, 1)
    now[0] = 3
    circuits.abandon(circuits.acquire("a"), 1)
    now[0] = 5
    probe = circuits.acquire("a")
    circuits.failure(probe, 1, 1)
    with pytest.raises(CircuitOpen):
        circuits.acquire("a")


@pytest.fixture
def configured(tmp_path, monkeypatch):
    import fitlife.execution as execution

    monkeypatch.setattr(execution, "CIRCUITS", Circuits())
    p = tmp_path / "models.json"
    p.write_text(
        json.dumps(
            [
                {
                    "name": "local",
                    "provider": "ollama",
                    "base_url": "http://127.0.0.1:11435",
                    "api_family": "ollama",
                    "model": "qwen3:4b",
                    "key_env": "LOCAL_UNUSED",
                    "capabilities": ["text", "tools"],
                    "request_timeout": 1,
                }
            ]
        )
    )
    return Settings(
        model_profiles_file=p,
        fallback_profile="local",
        request_timeout=0.01,
        model_retries=0,
        circuit_failure_threshold=1,
    )


class Cloud:
    network_model = True

    def __init__(self, settings, error=None):
        self.settings, self.error, self.calls, self.cancelled = settings, error, 0, False

    async def complete(self, *args):
        self.calls += 1
        if self.error:
            raise self.error
        try:
            await asyncio.sleep(60)
        finally:
            self.cancelled = True


class Local:
    network_model = True

    def __init__(self, settings):
        self.settings = settings

    async def complete(self, *args):
        return Reply(text="local", usage={"total_tokens": 3}, response_model="qwen3:4b")

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_timeout_fallback_accounts_attempts_and_stays_local(configured, monkeypatch):
    monkeypatch.setattr("fitlife.model.create_model", Local)
    cloud, ledger, trace = Cloud(configured), Ledger(), []
    first = await complete(cloud, "", [], [], configured, ledger, trace, 1)
    second = await complete(cloud, "", [], [], configured, ledger, trace, 2)
    assert first.text == second.text == "local"
    assert cloud.calls == 1 and cloud.cancelled and ledger.calls == 3
    assert ledger.token_units > 6  # Unknown cloud usage remains reserved.
    assert sum(e["kind"] == "model" for e in trace) == 3
    assert trace[-1]["local_fallback"]


@pytest.mark.asyncio
async def test_write_capable_or_image_requests_do_not_use_text_fallback(configured, monkeypatch):
    def forbidden(_):
        raise AssertionError("Local model must not be created")

    monkeypatch.setattr("fitlife.model.create_model", forbidden)
    for tools, items, error in [
        ([{"name": "propose_activity"}], [], "REQUIRES_READ_ONLY_TOOLS"),
        (
            [],
            [{"role": "user", "content": [{"type": "input_image", "image_url": "synthetic"}]}],
            "IMAGES_NOT_SUPPORTED",
        ),
    ]:
        with pytest.raises(ExecutionError, match=error):
            await complete(Cloud(configured), "", items, tools, configured, Ledger(), [], 1)


@pytest.mark.asyncio
async def test_auth_errors_never_trigger_fallback(configured, monkeypatch):
    def forbidden(_):
        raise AssertionError("Do not mask invalid credentials")

    monkeypatch.setattr("fitlife.model.create_model", forbidden)
    request = httpx.Request("POST", "https://example.org")
    error = httpx.HTTPStatusError(
        "auth", request=request, response=httpx.Response(401, request=request)
    )
    cloud = Cloud(configured, error)
    with pytest.raises(httpx.HTTPStatusError):
        await complete(cloud, "", [], [], configured, Ledger(), [], 1)
    assert cloud.calls == 1


@pytest.mark.asyncio
async def test_circuit_open_skips_network_and_attempt_budget(configured):
    configured = configured.model_copy(update={"fallback_profile": None})
    cloud, ledger, trace = Cloud(configured), Ledger(), []
    with pytest.raises(TimeoutError):
        await complete(cloud, "", [], [], configured, ledger, trace, 1)
    with pytest.raises(CircuitOpen):
        await complete(cloud, "", [], [], configured, ledger, trace, 2)
    assert cloud.calls == ledger.calls == 1
    assert trace[-1]["network_call"] is False


@pytest.mark.asyncio
async def test_fallback_cannot_reset_model_call_budget(configured, monkeypatch):
    monkeypatch.setattr("fitlife.model.create_model", Local)
    configured = configured.model_copy(update={"max_model_calls": 1})
    ledger = Ledger()
    with pytest.raises(ExecutionError, match="MODEL_CALL_BUDGET_EXHAUSTED"):
        await complete(Cloud(configured), "", [], [], configured, ledger, [], 1)
    assert ledger.calls == 1


@pytest.mark.asyncio
async def test_outer_cancellation_is_not_retried_or_failed_over(configured, monkeypatch):
    def forbidden(_):
        raise AssertionError("Cancelled caller must not start a local request")

    monkeypatch.setattr("fitlife.model.create_model", forbidden)
    configured = configured.model_copy(update={"request_timeout": 10})
    cloud, ledger = Cloud(configured), Ledger()
    task = asyncio.create_task(complete(cloud, "", [], [], configured, ledger, [], 1))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cloud.calls == ledger.calls == 1 and cloud.cancelled


def test_responses_tool_history_converts_without_reexecuting_tools():
    messages = chat_messages(
        "rule",
        [
            {"type": "reasoning", "id": "opaque"},
            {
                "type": "function_call",
                "call_id": "c1",
                "name": "lookup_steps_tool",
                "arguments": "{}",
            },
            {"type": "function_call_output", "call_id": "c1", "output": '{"steps":7}'},
        ],
    )
    assert messages[1]["tool_calls"][0]["id"] == messages[2]["tool_call_id"] == "c1"
    assert messages[2]["content"] == '{"steps":7}'


@pytest.mark.asyncio
async def test_ollama_adapter_passes_observations_and_disables_thinking():
    captured = []

    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "done": True,
                "model": "qwen3:4b",
                "message": {"role": "assistant", "content": "7步"},
                "prompt_eval_count": 10,
                "eval_count": 2,
            },
        )

    model = OllamaModel(Settings(base_url="http://127.0.0.1:11435", api_family="ollama"))
    await model.client.aclose()
    model.client = httpx.AsyncClient(
        base_url="http://127.0.0.1:11435/", transport=httpx.MockTransport(handler)
    )
    try:
        reply = await model.complete(
            "rule",
            [
                {
                    "type": "function_call",
                    "call_id": "c1",
                    "name": "lookup_steps_tool",
                    "arguments": "{}",
                },
                {"type": "function_call_output", "call_id": "c1", "output": '{"steps":7}'},
            ],
            [],
        )
        assert reply.text == "7步" and reply.usage["total_tokens"] == 12
        assert captured[0]["think"] is False
        assert captured[0]["messages"][-1]["tool_name"] == "lookup_steps_tool"
    finally:
        await model.close()


def test_routing_rules_choose_profiles_by_domain_kind_and_history(tmp_path):
    profiles = tmp_path / "profiles.json"
    profiles.write_text(
        json.dumps(
            [
                {
                    "name": "fast",
                    "provider": "example",
                    "base_url": "https://example.org",
                    "model": "small",
                    "key_env": "EXAMPLE",
                }
            ]
        )
    )
    rules = tmp_path / "rules.json"
    rules.write_text(
        json.dumps(
            [
                {
                    "name": "body-query",
                    "profile": "fast",
                    "domains": ["body"],
                    "kinds": ["query"],
                    "allow_history": True,
                }
            ]
        )
    )
    settings = Settings(
        routing_policy="rules", routing_rules_file=rules, model_profiles_file=profiles
    )
    plan = Plan(agents=["body"], tasks={"body": "query"}, covers={"body": ["q"]})
    req = [Requirement(id="q", domain="body", kind="query", text="查询")]
    assert select_model(settings, plan, "body", req, [{}])["model"] == "fast"
    req = [Requirement(id="q", domain="body", kind="deliverable", text="记录")]
    assert select_model(settings, plan, "body", req, [])["model"] == "standard"


@pytest.mark.asyncio
async def test_custom_standard_profile_reaches_factory_and_run_metadata(tmp_path, monkeypatch):
    import fitlife.agent as agent

    path = tmp_path / "profiles.json"
    path.write_text(
        json.dumps(
            [
                {
                    "name": "standard",
                    "provider": "fixture-provider",
                    "base_url": "https://example.org/v1",
                    "api_family": "chat",
                    "model": "fixture-model",
                    "key_env": "FIXTURE_KEY",
                    "reasoning_effort": "low",
                    "request_timeout": 7,
                }
            ]
        )
    )
    monkeypatch.setenv("FITLIFE_MODEL_PROFILES_FILE", str(path))
    monkeypatch.setattr(agent, "PROJECT_ROOT", tmp_path)
    captured = []

    def factory(settings):
        captured.append(settings)
        return Local(settings)

    async def execute(*args, **kwargs):
        return {"answer": "ok", "end_reason": "completed", "steps": 0}

    monkeypatch.setattr(agent, "execute", execute)
    result = await agent.run("配置验收", model_factory=factory)
    assert captured[0].model == result["requested_model"] == "fixture-model"
    assert captured[0].provider == result["provider"] == "fixture-provider"
    assert captured[0].request_timeout == 7
    assert captured[0].reasoning_effort == "low"
