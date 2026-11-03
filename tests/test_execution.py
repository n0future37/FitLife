import asyncio
from types import SimpleNamespace

import pytest

from fitlife.agent import RuntimeContext, call_tool, read_batch
from fitlife.execution import ExecutionError, Ledger, complete
from fitlife.model import Reply, Settings


class ProviderError(Exception):
    def __init__(self, status):
        self.status_code = status


class Model:
    def __init__(self, failures=0, status=503):
        self.calls, self.failures, self.status = 0, failures, status

    async def complete(self, *args):
        self.calls += 1
        if self.calls <= self.failures:
            raise ProviderError(self.status)
        return Reply(text="ok", usage={"total_tokens": 5})


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
async def test_transient_errors_retry_once_and_account_for_unknown_usage(status):
    model, ledger, trace = Model(1, status), Ledger(), []
    result = await complete(model, "", [], [], Settings(), ledger, trace, 1)
    assert result.text == "ok" and model.calls == 2 and len(trace) == 2
    assert trace[0]["retry_scheduled"] and ledger.token_units > 5


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403, 404])
async def test_permanent_errors_are_not_retried(status):
    model, trace = Model(2, status), []
    with pytest.raises(ProviderError):
        await complete(model, "", [], [], Settings(), Ledger(), trace, 1)
    assert model.calls == 1 and not trace[0]["retry_scheduled"]


@pytest.mark.asyncio
async def test_retry_exhaustion_and_model_call_limit():
    model, ledger = Model(99), Ledger()
    with pytest.raises(ProviderError):
        await complete(model, "", [], [], Settings(), ledger, [], 1)
    assert model.calls == 2
    with pytest.raises(ExecutionError, match="MODEL_CALL_BUDGET_EXHAUSTED"):
        await complete(model, "", [], [], Settings(max_model_calls=2), ledger, [], 2)
    assert model.calls == 2


@pytest.mark.asyncio
async def test_preflight_limits_block_network_calls():
    model = Model()
    with pytest.raises(ExecutionError, match="INPUT_BYTE_BUDGET_EXHAUSTED"):
        await complete(model, "x" * 1001, [], [], Settings(max_input_bytes=1000), Ledger(), [], 1)
    with pytest.raises(ExecutionError, match="TOKEN_UNIT_BUDGET_EXHAUSTED"):
        await complete(model, "", [], [], Settings(max_total_token_units=1000), Ledger(), [], 1)
    assert model.calls == 0


class Tool:
    def __init__(self, timeouts=0, error=False):
        self.calls, self.timeouts, self.error = 0, timeouts, error

    async def call_tool(self, *args, **kwargs):
        self.calls += 1
        if self.calls <= self.timeouts:
            await asyncio.sleep(1)
        return SimpleNamespace(
            isError=self.error,
            structuredContent={"status": "ok"},
            content=[SimpleNamespace(type="text", text="个人参数不应回送或记录")],
        )


@pytest.mark.asyncio
async def test_read_timeout_retries_but_candidate_timeout_does_not():
    settings, trace = Settings(tool_timeout=0.01), []
    readonly = Tool(1)
    call = {"name": "get_user_profile", "arguments": "{}"}
    result = await call_tool(readonly, call, RuntimeContext(private=True), settings, trace, 1)
    assert result["status"] == "ok" and readonly.calls == 2
    assert trace[0]["error_code"] == "TOOL_TIMEOUT" and trace[0]["retry_scheduled"]
    write = Tool(1)
    with pytest.raises(ExecutionError, match="TOOL_TIMEOUT"):
        await call_tool(
            write, {**call, "name": "propose_activity"}, RuntimeContext(), settings, [], 1
        )
    assert write.calls == 1


@pytest.mark.asyncio
async def test_read_retry_exhaustion_records_every_attempt():
    tool, trace = Tool(99), []
    with pytest.raises(ExecutionError, match="TOOL_TIMEOUT"):
        await call_tool(
            tool,
            {"name": "get_user_profile", "arguments": "{}"},
            RuntimeContext(),
            Settings(tool_timeout=0.01),
            trace,
            1,
        )
    assert len(trace) == 2 and not trace[-1]["retry_scheduled"]


@pytest.mark.asyncio
async def test_bad_json_never_executes_and_mcp_error_text_is_redacted():
    tool, trace = Tool(error=True), []
    output = await call_tool(
        tool,
        {"name": "propose_activity", "arguments": "{bad"},
        RuntimeContext(private=True),
        Settings(),
        trace,
        1,
    )
    assert tool.calls == 0 and output["error_code"] == "INVALID_TOOL_ARGUMENTS"
    output = await call_tool(
        tool,
        {"name": "propose_activity", "arguments": "{}"},
        RuntimeContext(private=True),
        Settings(),
        trace,
        2,
    )
    assert output["error_code"] == "INVALID_TOOL_ARGUMENTS"
    assert all(
        t["arguments"] == {"redacted": True} and t["result"] == {"redacted": True} for t in trace
    )


@pytest.mark.asyncio
async def test_read_batch_is_bounded_and_preserves_result_binding():
    class ParallelTool:
        def __init__(self):
            self.active = self.peak = 0

        async def call_tool(self, name, arguments):
            self.active += 1
            self.peak = max(self.peak, self.active)
            try:
                await asyncio.sleep(0.01)
                return SimpleNamespace(
                    isError=False, structuredContent={"status": "ok", "tool": name}
                )
            finally:
                self.active -= 1

    names = ["get_user_profile", "query_activities", "get_current_plan"]
    tool, trace = ParallelTool(), []
    calls = [{"name": n, "arguments": "{}"} for n in names]
    outputs = await read_batch(
        tool, calls, RuntimeContext(allowed_tools=frozenset(names)), Settings(), trace, 1
    )
    assert tool.peak == 2 and tool.active == 0
    assert [r["tool"] for r in outputs] == names
    assert all(t["name"] == t["result"]["tool"] for t in trace)
    with pytest.raises(ExecutionError, match="PARALLEL_READ_ONLY_REQUIRED"):
        await read_batch(tool, [{"name": "propose_activity"}], RuntimeContext(), Settings(), [], 1)


@pytest.mark.asyncio
async def test_public_catalog_invalid_arguments_cannot_leak_personal_text():
    tool, trace = Tool(), []
    await call_tool(
        tool,
        {"name": "get_activity_catalog", "arguments": '{"category":"合成个人资料"}'},
        RuntimeContext(),
        Settings(),
        trace,
        1,
    )
    assert trace[0]["arguments"] == {"redacted": True} and "合成个人资料" not in str(trace)
    trace.clear()
    await call_tool(
        tool,
        {"name": "get_activity_catalog", "arguments": '{"category":"all"}'},
        RuntimeContext(),
        Settings(),
        trace,
        1,
    )
    assert trace[0]["arguments"] == {"category": "all"}
