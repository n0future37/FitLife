"""主从编排可复现评测：同一初态与副作用规则，固定响应和真实模型分开运行。"""

import argparse
import hashlib
import json
import os
import re
import time
from datetime import timezone
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from fitlife.api import create_app
from fitlife.api_check import confirmation, headers
from fitlife.auth import issue_token
from fitlife.business import today
from fitlife.database import Activity, AgentRequest, DatabaseSettings, DomainRecord, Profile
from fitlife.domains import DomainService
from fitlife.evaluate import clean
from fitlife.evaluation_budget import PaidBudget
from fitlife.manage import migrate
from fitlife.model import Reply

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "evals/multiagent-v1.jsonl"


def facts(engine, actor):
    with Session(engine) as session:
        owner = str(actor.user_id)
        profile = session.get(Profile, owner)
        records = session.scalars(
            select(DomainRecord)
            .where(DomainRecord.owner_id == owner)
            .order_by(DomainRecord.record_type, DomainRecord.record_key)
        ).all()
        activities = session.scalars(
            select(Activity)
            .where(Activity.owner_id == owner)
            .order_by(Activity.occurred_on, Activity.minutes)
        ).all()
        return {
            "profile": profile.values if profile else {},
            "profile_version": profile.version if profile else 0,
            "records": [
                {"type": r.record_type, "version": r.version, "values": r.values} for r in records
            ],
            "activities": [
                {"activity_id": a.activity_id, "minutes": a.minutes, "date": str(a.occurred_on)}
                for a in activities
            ],
        }


class CaseModel:
    def __init__(self, case):
        self.case = case

    async def complete(self, instructions, items, tools):
        if not tools:
            if "意图路由器" in instructions:
                return Reply(text=json.dumps({"requirements": self.case["requirements"]}))
            if "规划三个领域" in instructions:
                return Reply(text=json.dumps(self.case["plan"]))
            return Reply(text="已按本轮工具证据汇总；候选尚未保存，未记录不等于实际为零。")
        scope = json.loads(next(i["content"] for i in reversed(items) if i.get("role") == "user"))
        script = self.case["script"][scope["domain"]]
        used = sum(i.get("type") == "function_call_output" for i in items)
        if used >= len(script):
            return Reply(text="已根据本轮查询结果完成本领域任务。")
        step = script[used]
        match = re.search(r"本轮上海日期：(\d{4}-\d{2}-\d{2})", instructions)
        request_day = match.group(1) if match else str(today())
        arguments = json.loads(json.dumps(step["arguments"]).replace("$today", request_day))
        call = {
            "type": "function_call",
            "name": step["tool"],
            "arguments": json.dumps(arguments),
            "call_id": str(uuid4()),
        }
        return Reply(calls=[call], continuation=[call], response_model="multi-fixed-v1")

    async def close(self):
        pass


def execute_case(engine, settings, case, *, live=False, budget=None, orchestration="master"):
    actor, token = issue_token(engine)
    other, _ = issue_token(engine)
    service = DomainService(engine)
    request_day = today()
    fixture_day = request_day
    result = {"id": case["id"], "group": case["group"], "checks": {}, "run_ids": []}
    try:
        for initial in case.get("initial", []):
            pending = service.propose_record(
                actor, initial["record_type"], initial["data"], str(uuid4()), today()
            )
            service.confirm(actor, pending["action_id"], pending["revision"], pending["digest"])
        own_before, other_before = facts(engine, actor), facts(engine, other)
        app = create_app(
            settings,
            engine=engine,
            orchestration_mode=orchestration,
            synthetic_evaluation=True,
            model_mode="live" if live else "fixed-response",
            model_factory=budget.factory if live else lambda _: CaseModel(case),
        )
        started = time.perf_counter()
        with TestClient(app) as client:
            key = uuid4()
            body = {"message": case["message"].replace("$today", str(request_day))}
            response = client.post("/agent/requests", headers=headers(token, key), json=body)
            result["http_status"] = response.status_code
            data = response.json()
            with Session(engine) as session:
                requested = session.scalar(
                    select(AgentRequest).where(
                        AgentRequest.owner_id == str(actor.user_id),
                        AgentRequest.request_key == str(key),
                    )
                )
                request_day = (
                    requested.created_at.replace(tzinfo=timezone.utc)
                    .astimezone(ZoneInfo("Asia/Shanghai"))
                    .date()
                )

            if data.get("run_id"):
                result["run_ids"].append(data["run_id"])
            result["checks"]["http_and_end_reason"] = (
                response.status_code == 200 and data.get("end_reason") == case["expected_end"]
            )
            result["checks"]["no_unconfirmed_fact_write"] = facts(engine, actor) == own_before
            pending = data.get("pending_action")
            result["checks"]["candidate_presence"] = bool(pending) == case.get("candidate", False)
            if pending:
                foreign = client.post(
                    f"/actions/{pending['action_id']}/confirm",
                    headers=headers(issue_token(engine, other.user_id)[1]),
                    json=confirmation(pending),
                )
                result["checks"]["foreign_confirmation_blocked"] = foreign.status_code == 404
                operation = case.get("control", "confirm")
                done = client.post(
                    f"/actions/{pending['action_id']}/{operation}",
                    headers=headers(token),
                    json=confirmation(pending),
                )
                result["checks"]["control_result"] = done.json().get("status") == (
                    "applied" if operation == "confirm" else "rejected"
                )
                after = facts(engine, actor)
                repeated = client.post(
                    f"/actions/{pending['action_id']}/{operation}",
                    headers=headers(token),
                    json=confirmation(pending),
                )
                result["checks"]["repeated_control_no_duplicate"] = (
                    repeated.status_code == 200 and facts(engine, actor) == after
                )
            cached = client.post("/agent/requests", headers=headers(token, key), json=body)
            result["checks"]["request_replay"] = (
                cached.status_code == response.status_code
                and cached.json().get("run_id") == data.get("run_id")
            )
        result["api_script_seconds"] = round(time.perf_counter() - started, 3)
        final = facts(engine, actor)
        expected = case["expected"]
        result["checks"]["activity_minutes"] = sum(
            r["minutes"] for r in final["activities"]
        ) == expected.get("activity_minutes", 0)
        result["checks"]["record_count"] = len(final["records"]) == expected.get("record_count", 0)
        result["checks"]["profile_values"] = final["profile"] == expected.get("profile", {})
        expected_activities = json.loads(
            json.dumps(expected.get("activities", [])).replace("$today", str(request_day))
        )
        result["checks"]["activity_values"] = final["activities"] == expected_activities
        result["checks"]["activity_count"] = len(final["activities"]) == expected.get(
            "activity_count", int(expected.get("activity_minutes", 0) > 0)
        )
        expected_values = json.loads(
            json.dumps(expected.get("record_values", {})).replace("$today", str(request_day))
        )
        if not case.get("candidate"):
            for values in expected_values.values():
                if "record_date" in values:
                    values["record_date"] = str(fixture_day)
        result["checks"]["record_values"] = all(
            any(r["type"] == kind and r["values"] == values for r in final["records"])
            for kind, values in expected_values.items()
        )

        result["checks"]["other_user_unchanged"] = facts(engine, other) == other_before
        result["checks"]["tool_allowlist"] = True
        result["checks"]["required_order"] = True
        reports = [
            json.loads((ROOT / "artifacts/runs" / f"{id_}.json").read_text(encoding="utf-8"))
            for id_ in result["run_ids"]
        ]
        calls = [
            e["name"]
            for report in reports
            for e in report["trace"]
            if e.get("kind") == "tool" and e.get("executed")
        ]
        result["checks"]["tool_allowlist"] = set(calls) <= set(case["allowed_tools"])
        sequence = case.get("required_order", [])
        if sequence:
            positions = [calls.index(tool) if tool in calls else -1 for tool in sequence]
            result["checks"]["required_order"] = -1 not in positions and positions == sorted(
                positions
            )

        def contains(actual, expected):
            if isinstance(expected, dict):
                return isinstance(actual, dict) and all(
                    k in actual and contains(actual[k], v) for k, v in expected.items()
                )
            if isinstance(expected, list):
                return (
                    isinstance(actual, list)
                    and len(actual) == len(expected)
                    and all(contains(a, e) for a, e in zip(actual, expected))
                )
            return actual == expected

        tool_events = [
            e
            for report in reports
            for e in report["trace"]
            if e.get("kind") == "tool" and e.get("executed")
        ]
        expected_calls = [
            step
            for script in case["script"].values()
            for step in script
            if step["tool"] in case["allowed_tools"]
        ]
        result["tool_parameter_check_applicable"] = bool(expected_calls)
        result["checks"]["tool_parameters"] = all(
            any(
                e["name"] == step["tool"]
                and not e.get("is_error")
                and contains(
                    e.get("arguments"),
                    json.loads(json.dumps(step["arguments"]).replace("$today", str(request_day))),
                )
                for e in tool_events
            )
            for step in expected_calls
        )
        result["executed_tools"] = calls
        result["model_attempts"] = sum(
            e.get("kind") == "model" for report in reports for e in report["trace"]
        )
        result["passed"] = all(result["checks"].values())
        return result
    finally:
        clean(engine, [actor, other])


def main():
    parser = argparse.ArgumentParser(description="主从编排评测，默认固定响应；--live会产生模型费用")
    parser.add_argument("--split", choices=["dev", "holdout"], default="dev")
    parser.add_argument(
        "--orchestration",
        choices=["master", "master_serial", "master_independent"],
        default="master",
    )
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--case")
    parser.add_argument("--budget-file", type=Path)
    parser.add_argument("--budget-usd", default="1.5")
    args = parser.parse_args()
    if args.live and not args.budget_file:
        raise SystemExit("--live requires a shared --budget-file")
    cases = [
        json.loads(line) for line in DATA.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    cases = [
        c for c in cases if c["split"] == args.split and (not args.case or c["id"] == args.case)
    ]
    if not cases:
        raise SystemExit("NO_MATCHING_CASE")
    settings = DatabaseSettings(environment="test")
    engine = settings.engine()
    migrate(engine, "test")
    budget = PaidBudget(args.budget_file, args.budget_usd) if args.live else None
    folder = ROOT / "artifacts/multi-evaluations" / str(uuid4())
    folder.mkdir(parents=True)
    report = {
        "mode": "live" if args.live else "fixed-response",
        "dataset_version": "multiagent-v1",
        "dataset_sha256": hashlib.sha256(DATA.read_bytes()).hexdigest(),
        "split": args.split,
        "orchestration": args.orchestration,
        "source_sha256": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((ROOT / "fitlife").glob("*.py"))
        },
        "environment": {
            k: v
            for k, v in os.environ.items()
            if k in {"FITLIFE_REASONING_EFFORT", "FITLIFE_ROUTING_POLICY", "FITLIFE_ORCHESTRATION"}
        },
        "results": [],
        "actual_cost_usd": None,
        "full_semantic_task_success_rate": None,
    }
    try:
        for case in cases:
            report["results"].append(
                execute_case(
                    engine,
                    settings,
                    case,
                    live=args.live,
                    budget=budget,
                    orchestration=args.orchestration,
                )
            )
        report["passed"] = sum(r["passed"] for r in report["results"])
        report["sample_count"] = len(cases)
        report["rule_pass_rate"] = report["passed"] / len(cases)
    finally:
        engine.dispose()
        (folder / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(
        f"Multi-agent {report['mode']}: {report['passed']}/{len(cases)}; {folder / 'report.json'}"
    )
    if report["passed"] != len(cases):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
