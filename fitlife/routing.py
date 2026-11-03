"""移植旧 Fit router 的清单/覆盖/DAG约束；模型和凭据由新版适配层提供。"""

import json
import re
from datetime import date
from typing import Literal

from pydantic import Field, model_validator

from fitlife.business import Input
from fitlife.execution import ExecutionError
from fitlife.registry import DOMAIN_READ_TOOLS, DOMAIN_TOOLS

Domain = Literal["body", "diet", "exercise"]


class Requirement(Input):
    id: str = Field(min_length=1, max_length=40)
    domain: Literal["body", "diet", "exercise", "main"]
    kind: Literal["query", "deliverable"]
    text: str = Field(min_length=1, max_length=4000)
    required_tools: list[str] = Field(default_factory=list, max_length=12)


class Requirements(Input):
    requirements: list[Requirement] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def valid_catalog(self):
        ids = [r.id for r in self.requirements]
        if len(ids) != len(set(ids)) or all(r.domain == "main" for r in self.requirements):
            raise ValueError("INVALID_REQUIREMENT_IDS_OR_DOMAINS")
        for r in self.requirements:
            if set(r.required_tools) - DOMAIN_TOOLS.get(r.domain, frozenset()):
                raise ValueError("REQUIREMENT_TOOL_OUTSIDE_DOMAIN")
        return self


class Plan(Input):
    agents: list[Domain] = Field(min_length=1, max_length=3)
    tasks: dict[Domain, str]
    depends_on: dict[Domain, list[Domain]] = Field(default_factory=dict)
    covers: dict[str, list[str]]


def json_object(raw):
    raw = raw.strip()
    if raw.startswith("```json") and raw.endswith("```"):
        raw = raw[7:-3].strip()
    elif raw.startswith("```") and raw.endswith("```"):
        raw = raw[3:-3].strip()
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise ValueError("JSON_OBJECT_REQUIRED")
    return result


def execution_order(plan):
    selected = set(plan.agents)
    if (
        len(selected) != len(plan.agents)
        or set(plan.tasks) != selected
        or set(plan.depends_on) - selected
    ):
        raise ValueError("INVALID_AGENT_SET")
    if any(not 1 <= len(t.strip()) <= 8000 for t in plan.tasks.values()):
        raise ValueError("INVALID_TASK")
    for name, parents in plan.depends_on.items():
        if name in parents or set(parents) - selected or len(parents) != len(set(parents)):
            raise ValueError("INVALID_DEPENDENCY")
    ordered = []
    while len(ordered) < len(selected):
        ready = next(
            (
                n
                for n in plan.agents
                if n not in ordered and set(plan.depends_on.get(n, [])) <= set(ordered)
            ),
            None,
        )
        if ready is None:
            raise ValueError("CYCLIC_DEPENDENCY")
        ordered.append(ready)
    return ordered


def check_coverage(plan, requirements):
    expected = {r.id: r.domain for r in requirements}
    actual = [(id_, domain) for domain, ids in plan.covers.items() for id_ in ids]
    if len(actual) != len(expected) or dict(actual) != expected:
        raise ValueError("INCOMPLETE_OR_DUPLICATE_COVERAGE")
    if set(plan.agents) != set(expected.values()) - {"main"}:
        raise ValueError("DOMAIN_COVERAGE_MISMATCH")
    execution_order(plan)
    return plan


def select_model(settings, plan, name, requirements, history):
    selected, reason = "standard", "fixed_domain_model"
    relevant = [r for r in requirements if r.domain == name]
    if settings.routing_policy == "conservative" and settings.easy_model:
        if (
            not history
            and len(plan.agents) == 1
            and not any(plan.depends_on.values())
            and all(
                r.kind == "query"
                and r.required_tools
                and set(r.required_tools) <= DOMAIN_READ_TOOLS
                for r in relevant
            )
            and not any(
                re.search(r"建议|推荐|分析|因果|估算|诊断|计划|修改|记录一下", r.text)
                for r in relevant
            )
        ):
            selected, reason = settings.easy_model, "single_domain_explicit_readonly_query"
        else:
            reason = "conservative_complex_or_history"
    return {"model": selected, "reason": reason, "policy": settings.routing_policy}


def invalid_range(message):
    for match in re.finditer(
        r"(\d{4}-\d{1,2}-\d{1,2})\s*(?:到|至|~|～|—)\s*((?:\d{4}-)?\d{1,2}-\d{1,2})(?!\d)", message
    ):
        first, last = match.groups()
        if len(last.split("-")) == 2:
            last = first[:4] + "-" + last
        try:
            if date.fromisoformat(first) > date.fromisoformat(last):
                return "开始日期晚于结束日期，请说明正确的起止日期；尚未查询，不能据此判断无记录。"
        except ValueError:
            return "日期范围含无效日期，请说明有效的起止日期；尚未查询。"
    return None


def safe_error(exc):
    if isinstance(exc, BaseExceptionGroup):
        codes = [safe_error(child) for child in exc.exceptions]
        return next((code for code in codes if code.endswith("EXHAUSTED")), codes[0])
    return str(exc) if isinstance(exc, ExecutionError) else type(exc).__name__
