from pathlib import Path

from fitlife.model import Settings
from fitlife.model_profiles import profiles, resolve
from fitlife.routing import Plan, Requirement, select_model


def test_shipped_siliconflow_routing_keeps_complex_and_history_on_strong_model():
    root = Path(__file__).resolve().parents[1]
    settings = Settings(
        model_profiles_file=root / "config/siliconflow.models.json",
        routing_policy="conservative",
        easy_model="fast",
    )
    catalog = profiles(settings)
    assert catalog["control"].model == "Qwen/Qwen3-32B"
    assert catalog["standard"].model == "Pro/deepseek-ai/DeepSeek-V3.2"
    assert catalog["complex"].enable_thinking is False
    assert catalog["local"].api_family == "ollama"
    assert all(catalog[n].request_timeout == 45 for n in ("standard", "complex"))
    assert all(catalog[n].request_timeout == 30 for n in ("fast", "control"))
    plan = Plan(agents=["exercise"], tasks={"exercise": "查询步数"}, covers={"exercise": ["r1"]})
    query = Requirement(
        id="r1",
        domain="exercise",
        kind="query",
        text="只查询步数，不做建议",
        required_tools=["lookup_steps_tool"],
    )

    def choose(p, r, history):
        return resolve(settings, select_model(settings, p, "exercise", r, history)["model"]).model

    assert choose(plan, [query], []) == "Qwen/Qwen3-8B"
    assert (
        choose(plan, [query], [{"role": "user", "content": "昨天呢"}]) == catalog["standard"].model
    )
    multi = plan.model_copy(
        update={"agents": ["body", "exercise"], "tasks": {"body": "查体重", "exercise": "查步数"}}
    )
    assert choose(multi, [query], []) == catalog["standard"].model
    deliverable = query.model_copy(update={"kind": "deliverable", "text": "生成计划"})
    assert choose(plan, [deliverable], []) == catalog["standard"].model
