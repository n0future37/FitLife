import json
from pathlib import Path

import pytest

from fitlife.experiments import profile_file
from fitlife.registry import DOMAIN_TOOLS

ROOT = Path(__file__).resolve().parents[1]


def cases():
    return [
        json.loads(line)
        for line in (ROOT / "evals/architecture-v1.jsonl").read_text(encoding="utf-8").splitlines()
    ]


def test_architecture_cases_use_equal_capabilities_and_complete_unified_scripts():
    data = cases()
    assert len(data) == 8 and len({c["id"] for c in data}) == 8
    assert {r["domain"] for c in data for r in c["requirements"]} == {"body", "diet", "exercise"}
    for case in data:
        assert case["experiment"] == "N4"
        assert set(case["unified_order"]) == set(case["script"]) == set(case["plan"]["agents"])
        assert all(
            s["tool"] in DOMAIN_TOOLS[d] for d, steps in case["script"].items() for s in steps
        )
        assert case["forbidden"] and case["cleanup"]
    groups = {}
    for case in data:
        groups.setdefault(case["group"], set()).add(case["split"])
    assert all(len(values) == 1 for values in groups.values())


@pytest.mark.parametrize("arm", ["unified", "multi"])
def test_architecture_models_and_timeouts_are_identical_in_every_role(tmp_path, arm):
    path = profile_file(tmp_path, cases()[0], arm, 45)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert {p["model"] for p in data} == {"Qwen/Qwen3-32B"}
    assert {p["request_timeout"] for p in data} == {45}
    assert all(p["temperature"] == 0 and p["enable_thinking"] is False for p in data)


@pytest.mark.parametrize("arm", ["unified", "multi"])
def test_replacement_model_is_shared_without_unsupported_thinking_parameter(tmp_path, arm):
    name = "Qwen/Qwen3-30B-A3B-Instruct-2507"
    path = profile_file(tmp_path, cases()[0], arm, 45, name)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert {p["model"] for p in data} == {name}
    assert {p["request_timeout"] for p in data} == {45}
    assert all(p["enable_thinking"] is None for p in data)


def test_core_selection_keeps_identical_cases_and_balances_domain_strata():
    path = ROOT / "evals/architecture-core-v1.jsonl"
    selected = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    old = {c["id"]: c for c in cases()}
    assert len(selected) == 6
    assert sum(c["stratum"] == "cross_domain" for c in selected) == 3
    for c in selected:
        for key in [
            "message",
            "initial",
            "script",
            "expected",
            "required_order",
            "answer_literals",
        ]:
            assert c[key] == old[c["id"]][key]


@pytest.mark.parametrize("arm", ["unified", "multi"])
def test_strong_comparison_model_is_same_in_all_roles(tmp_path, arm):
    name = "Pro/deepseek-ai/DeepSeek-V3.2"
    path = profile_file(tmp_path, cases()[0], arm, 60, name)
    profiles = json.loads(path.read_text(encoding="utf-8"))
    assert {p["model"] for p in profiles} == {name}
    assert all(p["request_timeout"] == 60 and p["enable_thinking"] is False for p in profiles)
