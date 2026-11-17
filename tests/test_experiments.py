import concurrent.futures
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from fitlife.api import create_app
from fitlife.context_policy import select_history
from fitlife.database import DatabaseSettings
from fitlife.experiment_budget import ExperimentBudget
from fitlife.experiments import expected_state, resolve_dates


def test_focused_history_keeps_complete_related_pairs_and_last_anchor():
    history = []
    for text in ("今天步行23分钟", "早餐要核对", "体重的单位是什么"):
        history.extend(
            [{"role": "user", "content": text}, {"role": "assistant", "content": "已说明"}]
        )
    assert select_history(history, "把刚才的步行记录下来", "focused") == history[:2] + history[-2:]
    assert select_history(history, "继续按刚才的来", "focused") == history
    assert select_history(history, "查询体重和步数", "focused") == history
    assert select_history(history, "步行", "none") == []
    assert select_history(history, "步行", "recent") == history


def test_experimental_context_controls_cannot_be_enabled_on_normal_app():
    with pytest.raises(ValueError, match="CONTEXT_EXPERIMENT_TEST_ONLY"):
        create_app(DatabaseSettings(environment="dev"), evaluation_history_policy="none")
    with pytest.raises(ValueError, match="CONTEXT_EXPERIMENT_TEST_ONLY"):
        create_app(DatabaseSettings(environment="dev"), evaluation_auto_titles=False)


def test_state_grader_catches_wrong_values_and_unexpected_profile_versions():
    before = {"profile": {}, "profile_version": 0, "records": [], "activities": [], "plans": {}}
    target = {"activity_id": "walking", "minutes": 23, "date": "2026-11-03"}
    final = {**before, "activities": [target]}
    assert expected_state(before, final, {"append_activities": [target]})
    assert not expected_state(
        before,
        {**final, "activities": [{**target, "minutes": 24}]},
        {"append_activities": [target]},
    )
    assert not expected_state(
        before, {**final, "profile_version": 1}, {"append_activities": [target]}
    )
    assert not expected_state(before, final, {"unchanged": True})


def test_dataset_is_group_disjoint_and_relative_dates_are_frozen():
    path = Path(__file__).resolve().parents[1] / "evals/experiments-v1.jsonl"
    data = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(data) == 12 and len({c["id"] for c in data}) == 12
    groups = {}
    for case in data:
        groups.setdefault(case["group"], set()).add(case["split"])
    assert all(len(s) == 1 for s in groups.values())
    frozen = resolve_dates({"x": "$D $Y $M $T $S $W"}, date(2026, 11, 3))
    assert frozen["x"] == "2026-11-03 2026-11-02 2026-11-09 2026-11-10 2026-11-14 2026-11-02"


def test_cross_process_budget_prevents_overspend_and_retains_unknown_usage(tmp_path):
    path = tmp_path / "budget.json"
    prices = {
        "fixture": {
            "context_length": 100000,
            "rates_cny_per_million": {"prompt": "1", "completion": "4"},
        }
    }
    budget = ExperimentBudget(path, prices, limit="0.2")
    code = """
import sys
from fitlife.experiment_budget import ExperimentBudget
from fitlife.model import Settings
prices={'fixture':{'context_length':100000,'rates_cny_per_million':{'prompt':'1','completion':'4'}}}
b=ExperimentBudget(sys.argv[1],prices,limit='0.2')
try:
 b.reserve(Settings(provider='siliconflow',base_url='https://api.siliconflow.cn/v1',model='fixture',max_output_tokens=1000))
 print('reserved')
except ValueError:
 print('blocked')
"""

    def worker(_):
        return subprocess.run(
            [sys.executable, "-c", code, str(path)], capture_output=True, text=True, check=True
        ).stdout.strip()

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(worker, range(3)))
    assert results.count("reserved") == 1 and results.count("blocked") == 2
    state = budget.read()
    assert len(state["attempts"]) == 1 and state["attempts"][0]["usage"] is None
