"""冻结任务集的数据库/轨迹评测；固定响应检验机制，真实模型用于小预算成对比较。"""

import argparse
import hashlib
import json
import os
import statistics
import time
import zipfile
from datetime import timedelta
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from fitlife.api import create_app
from fitlife.api_check import confirmation, headers
from fitlife.auth import issue_token
from fitlife.business import today
from fitlife.database import DatabaseSettings
from fitlife.evaluate import clean
from fitlife.evaluate_multi import facts
from fitlife.evaluation_budget import PaidBudget
from fitlife.manage import migrate
from fitlife.model import Reply, Settings
from fitlife.plan_types import PlanRequest
from fitlife.planning import PlanningService

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "evals/tasks-v06.1.jsonl"


def resolved(case):
    monday = today() - timedelta(days=today().weekday())
    tokens = {
        "$today": today(),
        "$this_monday": monday,
        "$next_monday": monday + timedelta(days=7),
        "$next_tuesday": monday + timedelta(days=8),
        "$next_saturday": monday + timedelta(days=12),
        "$next_sunday": monday + timedelta(days=13),
        "$this_sunday": monday + timedelta(days=6),
    }
    value = json.dumps(case, ensure_ascii=False)
    for key, day in sorted(tokens.items(), key=lambda pair: -len(pair[0])):
        value = value.replace(key, str(day))
    return json.loads(value)


class FixedTaskModel:
    def __init__(self, turn, domain):
        self.turn, self.domain = turn, domain

    async def complete(self, instructions, items, tools):
        if not tools:
            if "意图路由器" in instructions:
                return Reply(
                    text=json.dumps(
                        {
                            "requirements": [
                                dict(
                                    id="t1",
                                    domain=self.domain,
                                    kind=self.turn["kind"],
                                    text=self.turn["message"],
                                    required_tools=self.turn.get("required_reads", []),
                                )
                            ]
                        }
                    )
                )
            return Reply(text="已依据本轮实际工具结果处理；候选尚未确认，未记录不等于实际为零。")
        index = sum(i.get("type") == "function_call_output" for i in items)
        script = self.turn["tools"]
        if index >= len(script):
            return Reply(text="本轮工具已完成，数据以查询结果及确认状态为准。")
        step = script[index]
        call = dict(
            type="function_call",
            name=step["name"],
            arguments=json.dumps(step["arguments"]),
            call_id=str(uuid4()),
        )
        return Reply(calls=[call], continuation=[call], response_model="task-fixed-v06")

    async def close(self):
        pass


def apply(service, actor, action):
    return service.confirm(actor, action["action_id"], action["revision"], action["digest"])


def subset(value, wanted):
    if isinstance(wanted, dict):
        return isinstance(value, dict) and all(
            k in value and subset(value[k], v) for k, v in wanted.items()
        )
    return value == wanted


def execute(case, settings, engine, live=False, budget=None):
    case = resolved(case)
    actor, token = issue_token(engine)
    other, other_token = issue_token(engine)
    service = PlanningService(engine)
    checks, runs, old = {}, [], []
    result = dict(
        id=case["id"], group=case["group"], split=case["split"], checks=checks, run_ids=runs
    )
    turn_box = [case["turns"][0]]
    try:
        initial = case["initial"]
        if initial.get("profile"):
            apply(
                service,
                actor,
                service.propose(actor, "profile_patch", initial["profile"], str(uuid4())),
            )
        for week in initial.get("plans", []):
            from datetime import date

            apply(
                service,
                actor,
                service.propose_plan(
                    actor,
                    PlanRequest(week_start=date.fromisoformat(week), expected_plan_version=0),
                    str(uuid4()),
                ),
            )
        for activity in initial.get("activities", []):
            apply(service, actor, service.propose(actor, "record_activity", activity, str(uuid4())))
        before, other_before = facts(engine, actor), facts(engine, other)
        app = create_app(
            settings,
            engine=engine,
            orchestration_mode="master",
            synthetic_evaluation=True,
            model_mode="live" if live else "fixed-response",
            model_factory=budget.factory
            if live
            else lambda _: FixedTaskModel(turn_box[0], case["domain"]),
        )
        started = time.perf_counter()
        with TestClient(app) as client:
            conversation = client.post("/conversations", headers=headers(token)).json()[
                "conversation_id"
            ]
            for index, turn in enumerate(case["turns"]):
                turn_box[0] = turn
                key = uuid4()
                body = dict(message=turn["message"], conversation_id=conversation)
                response = client.post("/agent/requests", headers=headers(token, key), json=body)
                data = response.json()
                if data.get("run_id"):
                    runs.append(data["run_id"])
                checks[f"http_{index}"] = response.status_code == 200
                checks[f"no_unconfirmed_write_{index}"] = facts(engine, actor) == before
                candidate = data.get("pending_action")
                checks[f"candidate_{index}"] = bool(candidate) == turn.get("candidate", False)
                if candidate:
                    for prior in old:
                        rejected = client.post(
                            f"/actions/{prior['action_id']}/confirm",
                            headers=headers(token),
                            json=confirmation(prior),
                        )
                        checks[f"old_confirmation_blocked_{index}"] = (
                            rejected.status_code == 409
                            or rejected.json().get("status") == "superseded"
                        )
                    old.append(candidate)
                    foreign = client.post(
                        f"/actions/{candidate['action_id']}/confirm",
                        headers=headers(other_token),
                        json=confirmation(candidate),
                    )
                    checks[f"foreign_confirmation_{index}"] = foreign.status_code == 404
                # 中间轮先保留候选，下一轮应使其失效。
                if index == len(case["turns"]) - 1 and candidate:
                    saved = client.post(
                        f"/actions/{candidate['action_id']}/confirm",
                        headers=headers(token),
                        json=confirmation(candidate),
                    )
                    checks["confirmed"] = (
                        saved.status_code == 200 and saved.json().get("status") == "applied"
                    )
                    after = facts(engine, actor)
                    repeated = client.post(
                        f"/actions/{candidate['action_id']}/confirm",
                        headers=headers(token),
                        json=confirmation(candidate),
                    )
                    checks["duplicate_confirmation"] = (
                        repeated.status_code == 200 and facts(engine, actor) == after
                    )
                replay = client.post("/agent/requests", headers=headers(token, key), json=body)
                checks[f"request_replay_{index}"] = (
                    replay.status_code == response.status_code
                    and replay.json().get("run_id") == data.get("run_id")
                )
        result["seconds"] = round(time.perf_counter() - started, 3)
        final = facts(engine, actor)
        expected = case["expected"]
        checks["activity_count"] = len(final["activities"]) == expected["activity_count"]
        checks["activity_minutes"] = (
            sum(v["minutes"] for v in final["activities"]) == expected["activity_minutes"]
        )
        checks["profile"] = final["profile"] == expected["profile"]
        checks["record_count"] = len(final["records"]) == expected.get("record_count", 0)
        if "body_weight" in expected:
            checks["body_weight"] = any(
                r["type"] == "body"
                and r["values"].get("weight_kg") == expected["body_weight"]
                and r["values"].get("record_date") == str(today())
                for r in final["records"]
            )
        for week, target in expected.get("plans", {}).items():
            plan = service.get_plan(actor, week)
            checks["plan_" + week] = (
                plan["version"] == target["version"]
                and plan["plan"]["sessions"] == target["sessions"]
            )
        checks["other_user_unchanged"] = facts(engine, other) == other_before
        traces = [
            json.loads((ROOT / "artifacts/runs" / f"{id_}.json").read_text(encoding="utf-8"))
            for id_ in runs
        ]
        events = [event for report in traces for event in report["trace"]]
        calls = [e for e in events if e.get("kind") == "tool" and e.get("executed")]
        allowed = {step["name"] for turn in case["turns"] for step in turn["tools"]} | {
            "propose_action_bundle"
        }
        optional = case.get("optional_tools", [])
        allowed.update(step["name"] for step in optional)
        checks["tool_selection"] = {e["name"] for e in calls} <= allowed
        required_names = {step["name"] for turn in case["turns"] for step in turn["tools"]}
        checks["optional_read_scope"] = all(
            any(
                e["name"] == step["name"] and subset(e.get("arguments"), step["arguments"])
                for step in optional
            )
            for e in calls
            if e["name"] not in required_names | {"propose_action_bundle"}
        )
        for index, turn in enumerate(case["turns"]):
            per_turn = (
                [e for e in traces[index]["trace"] if e.get("kind") == "tool" and e.get("executed")]
                if index < len(traces)
                else []
            )
            positions = []
            for step in turn["tools"]:
                found = [
                    j
                    for j, e in enumerate(per_turn)
                    if e["name"] == step["name"] and subset(e.get("arguments"), step["arguments"])
                ]
                positions.append(found[0] if found else -1)
                if "expected_result" in step:
                    checks[f"result_{index}_{step['name']}"] = any(
                        e["name"] == step["name"]
                        and subset(e.get("result"), step["expected_result"])
                        for e in per_turn
                    )
            checks[f"parameters_and_order_{index}"] = -1 not in positions and positions == sorted(
                positions
            )
        result.update(
            tool_calls=len(calls),
            model_attempts=sum(e.get("kind") == "model" for e in events),
            steps=sum(r.get("steps", 0) for r in traces),
            selected_models=[e.get("response_model") for e in events if e.get("kind") == "model"],
            usage=[e.get("usage") for e in events if e.get("kind") == "model"],
            passed=all(checks.values()),
        )
    except Exception as exc:
        result.update(passed=False, error_type=type(exc).__name__)
    finally:
        clean(engine, [actor, other])
        result["cleanup"] = "verified"
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", choices=["dev", "holdout"], default="dev")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--routing", choices=["fixed", "conservative"], default="conservative")
    parser.add_argument("--repeat", type=int, choices=range(1, 4), default=1)
    args = parser.parse_args()
    data = [
        json.loads(line) for line in DATA.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    families = {}
    for case in data:
        families.setdefault(case["group"], set()).add(case["split"])
    assert all(len(splits) == 1 for splits in families.values()), "TASK_FAMILY_SPLIT_LEAKAGE"
    cases = [
        c for c in data if c["split"] == args.split and (not args.case or c["id"] in args.case)
    ]
    if not cases:
        raise SystemExit("NO_MATCHING_CASE")
    folder = ROOT / "artifacts/task-evaluations" / str(uuid4())
    folder.mkdir(parents=True)
    os.environ.update(
        FITLIFE_ROUTING_POLICY=args.routing,
        FITLIFE_REASONING_EFFORT="low",
        FITLIFE_MAX_OUTPUT_TOKENS="900",
    )
    settings = DatabaseSettings(environment="test")
    engine = settings.engine()
    migrate(engine, "test")
    budget = (
        PaidBudget(ROOT / "artifacts/paid-checks/budget-20261102.json", "1.5")
        if args.live
        else None
    )
    report = dict(
        mode="live" if args.live else "fixed-response",
        dataset="tasks-v06.1",
        dataset_sha256=hashlib.sha256(DATA.read_bytes()).hexdigest(),
        split=args.split,
        routing=args.routing,
        repeat=args.repeat,
        settings=Settings().model_dump(mode="json"),
        source_sha256={
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (ROOT / "fitlife").glob("*.py")
        },
        sdk_versions={name: version(name) for name in ["openai", "mcp", "SQLAlchemy"]},
        results=[],
        actual_billed_usd=None,
        full_semantic_success_rate=None,
    )
    try:
        for repeat in range(args.repeat):
            for case in cases:
                result = execute(case, settings, engine, args.live, budget)
                result["repeat"] = repeat + 1
                report["results"].append(result)
                print(
                    case["id"], repeat + 1, "passed" if result["passed"] else "failed", flush=True
                )
        times = [r["seconds"] for r in report["results"] if "seconds" in r]
        report.update(
            passed=sum(r["passed"] for r in report["results"]),
            n=len(report["results"]),
            unique_groups=len({c["group"] for c in cases}),
            latency=dict(
                n=len(times),
                mean=statistics.mean(times),
                min=min(times),
                max=max(times),
                population_stdev=statistics.pstdev(times),
                scope="API script includes controls/replay; excludes initial state and cleanup",
            ),
        )
    finally:
        if budget:
            report["shared_budget_held_usd"] = budget.state["reserved_usd"]
            report["shared_budget_effective_ceiling_used_usd"] = str(budget.effective_used())
        engine.dispose()
        (folder / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        with zipfile.ZipFile(folder / "source.zip", "w", zipfile.ZIP_DEFLATED) as archive:
            for path in [
                *(ROOT / "fitlife").glob("*.py"),
                *(ROOT / "fitlife/web").glob("*"),
                *(ROOT / "migrations").rglob("*.py"),
                DATA,
                ROOT / "alembic.ini",
                ROOT / "pyproject.toml",
            ]:
                if path.is_file():
                    archive.write(path, path.relative_to(ROOT).as_posix())
    print(f"Task rules: {report.get('passed')}/{report.get('n')}; {folder / 'report.json'}")
    if report.get("passed") != report.get("n"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
