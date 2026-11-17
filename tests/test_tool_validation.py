import json
from types import SimpleNamespace

import pytest

from fitlife.agent import RuntimeContext, call_tool
from fitlife.model import Settings
from fitlife.tool_validation import input_issues

SCHEMA = {
    "type": "object",
    "properties": {
        "start_date": {"type": "string", "format": "date"},
        "end_date": {"type": "string", "format": "date"},
    },
    "required": ["start_date", "end_date"],
    "additionalProperties": False,
}


def test_schema_feedback_contains_only_schema_fields_and_rules():
    args = {"start_date": "2026-11-03", "end_date": "2026--03", "ignore_all_rules_SECRET": "secret"}
    issues = input_issues(SCHEMA, args)
    assert {"field": "end_date", "rule": "format", "expected_format": "date"} in issues
    assert "secret" not in json.dumps(issues).lower()
    assert not input_issues(SCHEMA, {"start_date": "2026-11-03", "end_date": "2026-11-03"})
    assert input_issues(SCHEMA, {})[0]["rule"] == "required"


@pytest.mark.asyncio
async def test_bad_date_is_not_executed_and_corrected_call_uses_original_range():
    invoked = []

    class Session:
        async def call_tool(self, name, arguments):
            invoked.append(arguments)
            return SimpleNamespace(isError=False, structuredContent={"steps": 7312})

    context = RuntimeContext(
        private=True,
        tool_schemas={"lookup_steps_tool": SCHEMA},
        allowed_tools=frozenset({"lookup_steps_tool"}),
    )
    call = {
        "name": "lookup_steps_tool",
        "arguments": json.dumps({"start_date": "2026-11-03", "end_date": "2026--03"}),
    }
    trace = []
    bad = await call_tool(Session(), call, context, Settings(), trace, 1)
    assert bad["error_code"] == "INVALID_TOOL_ARGUMENTS"
    assert bad["invalid_parameters"][0]["field"] == "end_date" and not invoked
    assert not trace[-1]["executed"] and "2026" not in json.dumps(trace)
    good = {**call, "arguments": json.dumps({"start_date": "2026-11-03", "end_date": "2026-11-03"})}
    result = await call_tool(Session(), good, context, Settings(), trace, 2)
    assert result["steps"] == 7312 and len(invoked) == 1
