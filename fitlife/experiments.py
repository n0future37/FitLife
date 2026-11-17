"""冻结的N1/N2/N3实验，子进程隔离配置，真实API/MCP/MySQL及过程/副作用规则。"""

import argparse
import concurrent.futures
import hashlib
import json
import os
import platform
import statistics
import subprocess
import sys
import time
import zipfile
from datetime import date, datetime, timedelta
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from fitlife import conversations, requests
from fitlife.api import create_app
from fitlife.api_check import confirmation, headers
from fitlife.auth import issue_token
from fitlife.business import Actor, today
from fitlife.database import DatabaseSettings, Plan, PlanRevision, User
from fitlife.domains import DomainService
from fitlife.evaluate import clean
from fitlife.evaluate_multi import CaseModel, facts
from fitlife.experiment_budget import ExperimentBudget
from fitlife.experiment_contracts import tool_contract_matches
from fitlife.manage import migrate
from fitlife.model import Reply
from fitlife.plan_types import PlanRequest
from fitlife.planning import PlanningService
from fitlife.registry import DOMAIN_READ_TOOLS
from fitlife.siliconflow_check import public_prices
from fitlife.task_eval import subset

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "evals/experiments-v1.jsonl"
ARMS = {
    "N1": ["serial", "dag"],
    "N2": ["fixed_strong", "routed"],
    "N3": ["recent", "none", "focused"],
    "N4": ["unified", "multi"],
}


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def resolve_dates(case, anchor):
    monday = anchor - timedelta(days=anchor.weekday())
    days = {
        "$D": anchor,
        "$Y": anchor - timedelta(days=1),
        "$W": monday,
        "$M": monday + timedelta(days=7),
        "$T": monday + timedelta(days=8),
        "$S": monday + timedelta(days=12),
    }
    value = json.dumps(case, ensure_ascii=False)
    for key, day in days.items():
        value = value.replace(key, str(day))
    return json.loads(value)


def snapshot(engine, actor):
    result = facts(engine, actor)
    with Session(engine) as session:
        result["plans"] = {}
        for plan in session.scalars(select(Plan).where(Plan.owner_id == str(actor.user_id))):
            revision = session.get(PlanRevision, (plan.id, plan.version))
            result["plans"][str(plan.week_start)] = {
                "version": plan.version,
                "payload": revision.payload,
            }
    return result


def expected_state(before, final, expected):
    if expected.get("unchanged"):
        return final == before
    expected_activities = before["activities"] + expected.get("append_activities", [])
    if sorted(final["activities"], key=lambda x: json.dumps(x, sort_keys=True)) != sorted(
        expected_activities, key=lambda x: json.dumps(x, sort_keys=True)
    ):
        return False
    added = expected.get("append_records", [])
    if len(final["records"]) != len(before["records"]) + len(added):
        return False
    if any(
        not any(subset(r, wanted) for r in final["records"])
        for wanted in [*before["records"], *added]
    ):
        return False
    if final["profile"] != {**before["profile"], **expected.get("profile_updates", {})}:
        return False
    if (
        not expected.get("profile_updates")
        and final["profile_version"] != before["profile_version"]
    ):
        return False
    target = expected.get("plan")
    if not target:
        return final["plans"] == before["plans"]
    actual = final["plans"].get(target["week_start"], {})
    if (
        actual.get("version") != target["version"]
        or actual.get("payload", {}).get("sessions") != target["sessions"]
    ):
        return False
    return {k: v for k, v in final["plans"].items() if k != target["week_start"]} == {
        k: v for k, v in before["plans"].items() if k != target["week_start"]
    }


class FixedModel(CaseModel):
    async def complete(self, instructions, items, tools):
        if tools and "你是FitLife统一单Agent" in instructions:
            script = [
                step
                for domain in self.case["unified_order"]
                for step in self.case["script"][domain]
            ]
            used = sum(i.get("type") == "function_call_output" for i in items)
            if used < len(script):
                step = script[used]
                call = {
                    "type": "function_call",
                    "name": step["tool"],
                    "arguments": json.dumps(step["arguments"]),
                    "call_id": str(used),
                }
                return Reply(
                    calls=[call], continuation=[call], response_model="architecture-fixed-v1"
                )
            return Reply(
                text="；".join(self.case["answer_literals"]) or "全部任务已处理，候选尚未保存。",
                response_model="architecture-fixed-v1",
            )
        reply = await super().complete(instructions, items, tools)
        if (
            not reply.calls
            and "意图路由器" not in instructions
            and "规划三个领域" not in instructions
        ):
            text = "；".join(self.case["answer_literals"]) or "已按实际工具结果处理；候选尚未保存。"
            if self.case["expected"].get("ask_duration"):
                text = "这次步行实际用了多少分钟？"
            return Reply(text=text, response_model="experiment-fixed-v1")
        return reply


def profile_file(folder, case, arm, strong_request_timeout=30, architecture_model="Qwen/Qwen3-32B"):
    standard = architecture_model if case["experiment"] == "N4" else "Pro/deepseek-ai/DeepSeek-V3.2"
    fast = "Qwen/Qwen3-8B" if case["experiment"] == "N2" and arm == "routed" else standard
    profiles = [
        {
            "name": name,
            "provider": "siliconflow",
            "base_url": "https://api.siliconflow.cn/v1",
            "api_family": "chat",
            "model": model,
            "key_env": "SILI_API_KEY",
            "enable_thinking": False
            if ("Qwen3-" in model or "DeepSeek-V3.2" in model) and "Instruct" not in model
            else None,
            "capabilities": ["text", "tools"],
            "request_timeout": strong_request_timeout if model == standard else 30,
        }
        for name, model in (
            ("standard", standard),
            ("fast", fast),
            ("complex", standard),
            ("control", standard if case["experiment"] == "N4" else "Qwen/Qwen3-32B"),
        )
    ]
    for profile in profiles:
        profile["temperature"] = 0
    save(folder / "profiles.json", profiles)
    return folder / "profiles.json"


def episode(campaign, cell):
    config = json.loads((campaign / "protocol.json").read_text(encoding="utf-8"))
    for relative, digest in config["source_sha256"].items():
        if hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() != digest:
            raise ValueError("EXPERIMENT_SOURCE_CHANGED_AFTER_FREEZE")
    case = next(c for c in config["resolved_cases"] if c["id"] == cell["case"])
    folder = campaign / "episodes" / cell["id"]
    folder.mkdir(parents=True, exist_ok=True)
    profile = profile_file(
        folder,
        case,
        cell["arm"],
        config.get("strong_request_timeout", 30),
        config.get("architecture_model", "Qwen/Qwen3-32B"),
    )
    os.environ.update(
        {
            "FITLIFE_MODEL_PROFILES_FILE": str(profile),
            "FITLIFE_ROUTING_POLICY": config.get(
                "routing_policy",
                "rules" if case["experiment"] == "N2" and cell["arm"] == "routed" else "fixed",
            ),
            "FITLIFE_ROUTING_RULES_FILE": str(ROOT / "config/routing.example.json"),
            "FITLIFE_ROUTER_MODEL": "control",
            "FITLIFE_PLANNER_MODEL": "control",
            "FITLIFE_SYNTHESIS_MODEL": "control",
            "FITLIFE_EASY_MODEL": "fast",
            "FITLIFE_FALLBACK_PROFILE": "",
            "FITLIFE_MODEL_RETRIES": str(config.get("model_retries", 0)),
            "FITLIFE_MAX_OUTPUT_TOKENS": "1024",
            **(
                {
                    "FITLIFE_MAX_INPUT_BYTES": "150000",
                    "FITLIFE_MASTER_TOKEN_UNITS": "350000",
                    "FITLIFE_READONLY_CONCURRENCY": "2",
                    "FITLIFE_MASTER_MAX_TOOL_CALLS": "12",
                    "FITLIFE_FALLBACK_PROFILE": "",
                }
                if case["experiment"] == "N4"
                else {}
            ),
            "FITLIFE_MASTER_RUN_TIMEOUT": str(config.get("master_run_timeout", 120)),
            "FITLIFE_MASTER_MAX_MODEL_CALLS": "12",
            "FITLIFE_MASTER_MAX_STEPS": "16",
        }
    )
    db = DatabaseSettings(environment="test")
    engine = db.engine()
    actor, token = issue_token(engine)
    save(folder / "fixture-actors.json", [str(actor.user_id)])
    other, other_token = issue_token(engine)
    save(folder / "fixture-actors.json", [str(actor.user_id), str(other.user_id)])
    budget = (
        ExperimentBudget(
            Path(config["budget_file"]),
            config["prices"],
            limit=config.get("budget_limit_cny", "5"),
            tag=campaign.name + "/" + cell["id"],
            response_dir=folder / "model-replies",
        )
        if config["mode"] == "live"
        else None
    )
    result = {
        **cell,
        "experiment": case["experiment"],
        "split": case["split"],
        "group": case["group"],
        "checks": {},
        "run_ids": [],
        "mode": config["mode"],
        "elapsed_scope": (
            "agent HTTP request plus confirmation/replay controls; excludes fixture setup/cleanup"
        ),
    }
    checks = result["checks"]
    planning, domain = PlanningService(engine), DomainService(engine)
    try:
        initial = case["initial"]
        if initial.get("profile"):
            p = planning.propose(actor, "profile_patch", initial["profile"], str(uuid4()))
            planning.confirm(actor, p["action_id"], p["revision"], p["digest"])
        for record in initial.get("records", []):
            p = domain.propose_record(
                actor,
                record["type"],
                record["data"],
                str(uuid4()),
                date.fromisoformat(config["anchor_date"]),
            )
            domain.confirm(actor, p["action_id"], p["revision"], p["digest"])
        for week in initial.get("plans", []):
            p = planning.propose_plan(
                actor,
                PlanRequest(week_start=date.fromisoformat(week), expected_plan_version=0),
                str(uuid4()),
            )
            planning.confirm(actor, p["action_id"], p["revision"], p["digest"])
        p = domain.propose_record(
            other,
            "steps",
            {"steps": 198765, "record_date": config["anchor_date"]},
            str(uuid4()),
            date.fromisoformat(config["anchor_date"]),
        )
        domain.confirm(other, p["action_id"], p["revision"], p["digest"])
        cid = conversations.create(engine, actor)["conversation_id"]
        for message, answer in case["history"]:
            claim = requests.claim(engine, actor, uuid4(), message, 30, cid)
            requests.finish(engine, actor, claim["id"], claim["nonce"], {"answer": answer}, True)
        before, foreign_before = snapshot(engine, actor), snapshot(engine, other)
        save(
            folder / "initial.json",
            {"own": before, "other": foreign_before, "history": case["history"]},
        )
        policy = cell["arm"] if case["experiment"] == "N3" else "recent"
        mode = (
            "unified"
            if cell["arm"] == "unified"
            else "master_serial"
            if cell["arm"] == "serial"
            else "master"
        )
        app = create_app(
            db,
            engine=engine,
            synthetic_evaluation=True,
            orchestration_mode=mode,
            model_mode="experiment-" + config["mode"],
            model_factory=budget.factory if budget else lambda _: FixedModel(case),
            evaluation_history_policy=policy,
            evaluation_auto_titles=False,
        )
        with TestClient(app) as client:
            checks["foreign_conversation_blocked"] = (
                client.get(f"/conversations/{cid}", headers=headers(other_token)).status_code == 404
            )
            key = uuid4()
            body = {"message": case["message"], "conversation_id": cid}
            started = time.perf_counter()
            response = client.post("/agent/requests", headers=headers(token, key), json=body)
            data = response.json()
            result["http_status"] = response.status_code
            id_ = data.get("run_id") or (data.get("detail") or {}).get("run_id")
            if id_:
                result["run_ids"].append(id_)
            result["response"] = data
            checks["http_success"] = response.status_code == 200
            checks["end_reason"] = data.get("end_reason") == (
                "pending_confirmation" if case["candidate"] else "completed"
            )
            after_agent = snapshot(engine, actor)
            save(folder / "before-confirm.json", after_agent)
            checks["no_unconfirmed_write"] = after_agent == before
            pending = data.get("pending_action")
            checks["candidate_presence"] = bool(pending) == case["candidate"]
            if pending:
                url = f"/actions/{pending['action_id']}"
                foreign = client.post(
                    url + "/confirm", headers=headers(other_token), json=confirmation(pending)
                )
                checks["foreign_confirmation_blocked"] = foreign.status_code == 404
                stale = client.post(
                    url + "/confirm",
                    headers=headers(token),
                    json={**confirmation(pending), "revision": pending["revision"] + 1},
                )
                checks["wrong_confirmation_version_blocked"] = stale.status_code == 409
                operation = case["control"]
                outcome = client.post(
                    url + "/" + operation, headers=headers(token), json=confirmation(pending)
                )
                checks["control_result"] = outcome.status_code == 200 and outcome.json().get(
                    "status"
                ) == ("applied" if operation == "confirm" else "rejected")
                after_control = snapshot(engine, actor)
                repeat = client.post(
                    url + "/" + operation, headers=headers(token), json=confirmation(pending)
                )
                checks["duplicate_control_no_write"] = (
                    repeat.status_code == 200 and snapshot(engine, actor) == after_control
                )
            if response.status_code == 200:
                calls = budget.local_calls if budget else 0
                replay = client.post("/agent/requests", headers=headers(token, key), json=body)
                checks["cached_request_replay"] = (
                    replay.status_code == 200
                    and replay.json().get("run_id") == id_
                    and (budget.local_calls == calls if budget else True)
                )
            result["seconds"] = round(time.perf_counter() - started, 6)
        final = snapshot(engine, actor)
        save(folder / "final.json", final)
        checks["expected_final_state"] = expected_state(before, final, case["expected"])
        checks["other_user_unchanged"] = snapshot(engine, other) == foreign_before
        answer = str(data.get("answer", "")).replace(",", "").replace("，", "")
        checks["answer_anchors"] = all(v in answer for v in case["answer_literals"])
        if case["expected"].get("ask_duration"):
            checks["asks_duration"] = any(word in answer for word in ("分钟", "多久", "时长"))
        reports = []
        for id_ in result["run_ids"]:
            source = ROOT / "artifacts/runs" / (id_ + ".json")
            r = json.loads(source.read_text(encoding="utf-8"))
            reports.append(r)
            save(folder / (id_ + ".json"), r)
        events = [e for r in reports for e in r["trace"]]
        calls = [e for e in events if e.get("kind") == "tool" and e.get("executed")]
        wanted = [step for script in case["script"].values() for step in script]
        ordered = case["required_order"]
        checks["critical_order"] = True
        for left, right in zip(ordered, ordered[1:]):
            a = [e for e in calls if e["name"] == left and not e.get("is_error")]
            b = [e for e in calls if e["name"] == right and not e.get("is_error")]
            checks["critical_order"] &= bool(a and b) and min(e["finished_ns"] for e in a) <= min(
                e["started_ns"] for e in b
            )
        matches = [
            tool_contract_matches(
                step, calls, checks, case.get("tool_parameter_policy", "strict-v1")
            )
            for step in wanted
        ]
        checks["tool_parameter_contract"] = all(matches)
        graphs = [e for e in events if e.get("kind") == "route_agent"]
        required_edges = case["plan"]["depends_on"]
        if case["experiment"] != "N4":
            checks["dependency_graph_contract"] = all(
                any(parent in e.get("depends_on", {}).get(child, []) for e in graphs)
                for child, parents in required_edges.items()
                for parent in parents
            )
        result["planned_graphs"] = [e.get("depends_on", {}) for e in graphs]
        allowed = (
            DOMAIN_READ_TOOLS
            | {s["tool"] for s in wanted}
            | {"propose_action_bundle", "record_height_tool"}
        )
        checks["no_unexpected_write_tool"] = all(e["name"] in allowed for e in calls)
        models = [e for e in events if e.get("kind") == "model"]
        result["model_events"] = models
        result["tool_events"] = calls
        result["history_contexts"] = [r.get("history_context") for r in reports]
        result["delegate_intervals"] = [e for e in events if e.get("kind") == "delegate_agent"]
        result["model_attempts"] = len(models)
        result["tool_calls"] = len(calls)
        result["steps"] = (
            sum(r["steps"] for r in reports)
            if reports and all(type(r.get("steps")) is int for r in reports)
            else None
        )
        result["steps_known"] = result["steps"] is not None
        result["tool_contract_version"] = case.get("tool_parameter_policy", "strict-v1")
        result["tool_contract_matches"] = sum(matches)
        result["tool_contract_total"] = len(matches)
        result["passed"] = all(checks.values())
        outcome_keys = [
            k
            for k in checks
            if k not in {"tool_parameter_contract", "critical_order", "dependency_graph_contract"}
        ]
        result["outcome_passed"] = all(checks[k] for k in outcome_keys)
    except Exception as exc:
        result.update(passed=False, outcome_passed=False, error_type=type(exc).__name__)
    finally:
        clean(engine, [actor, other])
        with Session(engine) as session:
            result["cleanup_verified"] = (
                session.scalar(
                    select(User.id).where(User.id.in_([str(actor.user_id), str(other.user_id)]))
                )
                is None
            )
        if not result["cleanup_verified"]:
            result["passed"] = False
        if budget:
            result["budget_events"] = [
                e
                for e in budget.read()["attempts"]
                if e.get("episode") == campaign.name + "/" + cell["id"]
            ]
        engine.dispose()
        save(folder / "result.json", result)
    return result


def summary(campaign):
    config = json.loads((campaign / "protocol.json").read_text(encoding="utf-8"))
    results = [
        json.loads(p.read_text(encoding="utf-8"))
        for p in sorted((campaign / "episodes").glob("*/result.json"))
    ]
    groups = {}
    for r in results:
        key = r["experiment"] + "/" + r["arm"] + "/" + r["split"]
        groups.setdefault(key, []).append(r)
    output = {
        "mode": config["mode"],
        "dataset_sha256": config["dataset_sha256"],
        "source_sha256": config["source_sha256"],
        "n": len(results),
        "groups": {},
        "actual_billed_cny": None,
        "open_response_semantic_success": None,
        "limitations": [
            "synthetic small samples; strict executable contracts, not full open-answer quality",
            "3 repeats of same cases are not independent user samples",
            "cost from public prices and reported tokens; unknown calls retain budget holds",
            "latency includes API/MCP and controls; excludes setup/cleanup; no isolated TTFT",
            "all failed/timeouts retained; no claim that a proposed policy must win",
        ],
    }
    for key, values in groups.items():
        times = [r["seconds"] for r in values if "seconds" in r]
        models = [e for r in values for e in r.get("model_events", [])]
        known = [
            e["usage"]
            for e in models
            if e.get("usage") and isinstance(e["usage"].get("total_tokens"), int)
        ]
        costs = [e for r in values for e in r.get("budget_events", [])]
        output["groups"][key] = {
            "n": len(values),
            "rule_passed": sum(r["passed"] for r in values),
            "outcome_passed": sum(r["outcome_passed"] for r in values),
            "rule_success_rate": sum(r["passed"] for r in values) / len(values),
            "latency_seconds": {
                "n": len(times),
                "mean": statistics.mean(times) if times else None,
                "min": min(times) if times else None,
                "max": max(times) if times else None,
                "stdev": statistics.pstdev(times) if times else None,
            },
            "model_attempts": len(models),
            "model_usage_known": len(known),
            "known_total_tokens": sum(u["total_tokens"] for u in known),
            "total_tokens": sum(u["total_tokens"] for u in known)
            if len(known) == len(models)
            else None,
            "estimated_known_cny": str(
                sum(__import__("decimal").Decimal(e.get("estimated_cny", "0")) for e in costs)
            ),
            "cost_unknown_attempts": sum(e.get("usage") is None for e in costs),
            "tool_calls": sum(r.get("tool_calls", 0) for r in values),
            "known_steps": sum(r["steps"] for r in values if type(r.get("steps")) is int),
            "steps_known_episodes": sum(type(r.get("steps")) is int for r in values),
            "steps": sum(r["steps"] for r in values)
            if all(type(r.get("steps")) is int for r in values)
            else None,
            "unconfirmed_write_episodes": sum(
                r["checks"].get("no_unconfirmed_write") is False for r in values
            ),
            "other_user_changed_episodes": sum(
                r["checks"].get("other_user_unchanged") is False for r in values
            ),
            "duplicate_control_write_episodes": sum(
                r["checks"].get("duplicate_control_no_write") is False for r in values
            ),
            "safety_check_denominators": {
                key: sum(key in r["checks"] for r in values)
                for key in (
                    "no_unconfirmed_write",
                    "other_user_unchanged",
                    "duplicate_control_no_write",
                )
            },
            "contract_matches": sum(r.get("tool_contract_matches", 0) for r in values),
            "contract_total": sum(r.get("tool_contract_total", 0) for r in values),
            "failed_ids": [r["id"] for r in values if not r["passed"]],
        }
    output["results"] = results
    save(campaign / "summary.json", output)
    return output


def create_campaign(mode, repeats, case_ids, dataset=None, architecture_model="Qwen/Qwen3-32B"):
    dataset = (dataset or DATA).resolve()
    if not dataset.is_relative_to(ROOT / "evals") or dataset.suffix != ".jsonl":
        raise ValueError("DATASET_MUST_BE_PROJECT_EVAL_JSONL")
    cases = [
        json.loads(line)
        for line in dataset.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if case_ids:
        cases = [c for c in cases if c["id"] in case_ids]
    assert cases
    groups = {}
    for c in cases:
        groups.setdefault(c["group"], set()).add(c["split"])
    assert all(len(v) == 1 for v in groups.values()), "GROUP_SPLIT_LEAKAGE"
    campaign = ROOT / "artifacts/experiments-v1" / str(uuid4())
    campaign.mkdir(parents=True)
    db = DatabaseSettings(environment="test")
    engine = db.engine()
    migrate(engine, "test")
    engine.dispose()
    prices = (
        public_prices(
            {"Pro/deepseek-ai/DeepSeek-V3.2", "Qwen/Qwen3-8B", "Qwen/Qwen3-32B", architecture_model}
        )
        if mode == "live"
        else {}
    )
    source = {
        p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted([*(ROOT / "fitlife").glob("*.py"), ROOT / "config/routing.example.json"])
    }
    cells = []
    for split in ("dev", "holdout"):
        for repeat in range(repeats):
            for index, case in enumerate(c for c in cases if c["split"] == split):
                arms = ARMS[case["experiment"]]
                shift = (repeat + index) % len(arms)
                arms = arms[shift:] + arms[:shift]
                for arm in arms:
                    cells.append(
                        {
                            "id": f"{case['id']}--r{repeat + 1}--{arm}",
                            "case": case["id"],
                            "arm": arm,
                            "repeat": repeat + 1,
                        }
                    )
    anchor = today()
    config = {
        "created_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "mode": mode,
        "dataset_sha256": hashlib.sha256(dataset.read_bytes()).hexdigest(),
        "dataset_path": dataset.relative_to(ROOT).as_posix(),
        "anchor_date": str(anchor),
        "resolved_cases": [resolve_dates(c, anchor) for c in cases],
        "repeat": repeats,
        "schedule": cells,
        "source_sha256": source,
        "prices": prices,
        "budget_limit_cny": "5",
        "budget_file": str(ROOT / "artifacts/experiments-v1/budget-cny-v1.json"),
        "parallel_workers": 1,
        **(
            {
                "campaign_version": "architecture-v1",
                "architecture_model": architecture_model,
                "strong_request_timeout": 45,
                "model_retries": 1,
                "master_run_timeout": 180,
                "budget_limit_cny": "3",
                "budget_file": str(campaign / "budget-cny.json"),
                "dataset_role": f"exposed {len(cases)} templates; not a blind holdout",
                "primary_comparison": "same-model unified vs domain DAG",
                "shared_limits": {
                    "max_steps": 16,
                    "max_model_attempts": 12,
                    "max_tool_attempts": 12,
                    "max_input_bytes": 150000,
                    "token_units": 350000,
                    "readonly_concurrency": 2,
                    "output_tokens": 1024,
                    "whole_seconds": 180,
                    "request_seconds": 45,
                    "retries": 1,
                },
                "disabled": [
                    "model routing",
                    "local fallback",
                    "automatic titles",
                    "focused history",
                ],
                "scoring": "state, tools, order, anchors, safety; no graph score",
            }
            if all(c["experiment"] == "N4" for c in cases)
            else {}
        ),
        "episode_cooldown_seconds": 15 if mode == "live" else 0,
        "protocol": (
            "same case/repeat pairs; arm positions rotated; sequential episodes; "
            "DAG child concurrency 2; dev before frozen holdout"
        ),
        "environment": {
            "python": sys.version,
            "platform": platform.platform(),
            "versions": {
                p: version(p)
                for p in ("openai", "mcp", "fastapi", "SQLAlchemy", "httpx", "pydantic", "alembic")
            },
        },
    }
    save(campaign / "protocol.json", config)
    (campaign / "dataset.jsonl").write_bytes(dataset.read_bytes())
    with zipfile.ZipFile(campaign / "source.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for relative in source:
            archive.write(ROOT / relative, relative)
        archive.write(dataset, dataset.relative_to(ROOT).as_posix())
        for path in [
            *(ROOT / "fitlife/web").glob("*"),
            *(ROOT / "migrations").rglob("*.py"),
            ROOT / "pyproject.toml",
            ROOT / "alembic.ini",
        ]:
            if path.is_file() and "__pycache__" not in path.parts:
                archive.write(path, path.relative_to(ROOT).as_posix())
    if mode == "live":
        ExperimentBudget(Path(config["budget_file"]), prices, limit=config["budget_limit_cny"])
    return campaign, config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--repeat", type=int, choices=(1, 2, 3), default=3)
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--campaign", type=Path)
    parser.add_argument("--dataset", type=Path)
    parser.add_argument("--architecture-model", default="Qwen/Qwen3-32B")
    parser.add_argument("--worker", help="Internal isolated episode id")
    args = parser.parse_args()
    if args.worker:
        config = json.loads((args.campaign / "protocol.json").read_text(encoding="utf-8"))
        cell = next(c for c in config["schedule"] if c["id"] == args.worker)
        episode(args.campaign, cell)
        return
    if args.campaign:
        campaign = args.campaign
        config = json.loads((campaign / "protocol.json").read_text(encoding="utf-8"))
        if (config["mode"] == "live") != args.live:
            raise ValueError("RESUME_REQUIRES_MATCHING_EXPLICIT_LIVE_FLAG")
    else:
        campaign, config = create_campaign(
            "live" if args.live else "fixed-response",
            args.repeat,
            args.case,
            args.dataset,
            args.architecture_model,
        )
    print("CAMPAIGN " + str(campaign), flush=True)
    for relative, digest in config["source_sha256"].items():
        if hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() != digest:
            raise ValueError("EXPERIMENT_SOURCE_CHANGED_AFTER_FREEZE")
    last_finished = [None]

    def run_cell(cell):
        folder = campaign / "episodes" / cell["id"]
        if (folder / "result.json").exists():
            return json.loads((folder / "result.json").read_text(encoding="utf-8"))
        wait_started = time.monotonic()
        if last_finished[0] is not None:
            time.sleep(
                max(
                    0,
                    config.get("episode_cooldown_seconds", 15)
                    - (time.monotonic() - last_finished[0]),
                )
            )
        wait_seconds = time.monotonic() - wait_started
        try:
            outcome = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "fitlife.experiments",
                    "--campaign",
                    str(campaign),
                    "--worker",
                    cell["id"],
                ],
                cwd=ROOT,
                env=os.environ.copy(),
                capture_output=True,
                timeout=config.get("master_run_timeout", 120) + 30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            exit_code = outcome.returncode
        except subprocess.TimeoutExpired:
            exit_code = -1
        last_finished[0] = time.monotonic()
        if not (folder / "result.json").exists():
            fallback = {
                **cell,
                "experiment": next(
                    c["experiment"] for c in config["resolved_cases"] if c["id"] == cell["case"]
                ),
                "split": next(
                    c["split"] for c in config["resolved_cases"] if c["id"] == cell["case"]
                ),
                "mode": config["mode"],
                "passed": False,
                "outcome_passed": False,
                "checks": {},
                "worker_exit": exit_code,
                "error_type": "WorkerDidNotProduceResult",
                "cleanup_verified": False,
            }
            actors_path = folder / "fixture-actors.json"
            if actors_path.exists():
                from uuid import UUID

                actors = [
                    Actor(UUID(v)) for v in json.loads(actors_path.read_text(encoding="utf-8"))
                ]
                orphan_engine = DatabaseSettings(environment="test").engine()
                try:
                    clean(orphan_engine, actors)
                    with Session(orphan_engine) as session:
                        fallback["cleanup_verified"] = (
                            session.scalar(
                                select(User.id).where(User.id.in_([str(a.user_id) for a in actors]))
                            )
                            is None
                        )
                finally:
                    orphan_engine.dispose()
            save(folder / "result.json", fallback)
        result = json.loads((folder / "result.json").read_text(encoding="utf-8"))
        result["outside_measurement_wait_seconds"] = round(wait_seconds, 3)
        save(folder / "result.json", result)
        return result

    count = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        for split in ("dev", "holdout"):
            cells = [
                c
                for c in config["schedule"]
                if next(k["split"] for k in config["resolved_cases"] if k["id"] == c["case"])
                == split
            ]
            futures = {pool.submit(run_cell, c): c for c in cells}
            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                count += 1
                print(
                    json.dumps(
                        {
                            "completed": count,
                            "total": len(config["schedule"]),
                            "id": result["id"],
                            "passed": result["passed"],
                            "seconds": result.get("seconds"),
                            "failed_checks": [k for k, v in result["checks"].items() if not v],
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                summary(campaign)
    report = summary(campaign)
    print("SUMMARY " + str(campaign / "summary.json") + f"; episodes={report['n']}", flush=True)


if __name__ == "__main__":
    main()
