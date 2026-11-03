"""规则优先的合成评测。真实模型与固定响应严格分开，费用未知保持null。"""

import argparse
import asyncio
import hashlib
import json
import platform
import statistics
import time
import zipfile
from datetime import timedelta
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from fitlife.api import create_app
from fitlife.api_check import confirmation, headers
from fitlife.auth import issue_token
from fitlife.business import today
from fitlife.database import (
    AccessToken,
    Activity,
    AgentRequest,
    Conversation,
    DatabaseSettings,
    PendingAction,
    Plan,
    PlanRevision,
    Profile,
    User,
)
from fitlife.manage import migrate
from fitlife.model import Reply, ResponsesModel, Settings
from fitlife.plan_types import PlanRequest
from fitlife.planning import PlanningService

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "evals/cases.jsonl"


class InjectedProviderError(Exception):
    def __init__(self, status):
        self.status_code = status


class FixtureModel:
    def __init__(self, script):
        self.script, self.index = script, 0

    async def complete(self, instructions, items, tools):
        if self.index >= len(self.script):
            raise RuntimeError("FIXTURE_EXHAUSTED")
        event = self.script[self.index]
        self.index += 1
        if "error_status" in event:
            raise InjectedProviderError(event["error_status"])
        if "sleep" in event:
            await asyncio.sleep(event["sleep"])
        if "tool" not in event:
            return Reply(text=event["text"], response_model="dataset-fixture-v1")
        call = {
            "type": "function_call",
            "name": event["tool"],
            "arguments": event.get("raw_args", json.dumps(event.get("args", {}))),
            "call_id": f"fixture-{self.index}",
        }
        return Reply(calls=[call], continuation=[call], response_model="dataset-fixture-v1")

    async def close(self):
        pass


class ModelBudget:
    def __init__(self, limit):
        self.limit, self.calls = limit, 0

    def wrap(self, model):
        budget = self

        class Wrapped:
            async def complete(self, *args):
                if budget.calls >= budget.limit:
                    raise RuntimeError("EVALUATION_MODEL_CALL_LIMIT")
                budget.calls += 1
                return await model.complete(*args)

            async def close(self):
                await model.close()

        return Wrapped()


def subset(actual, expected):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            k in actual and subset(actual[k], value) for k, value in expected.items()
        )
    return actual == expected


def state(engine, actor):
    owner = str(actor.user_id)
    with Session(engine) as session:
        profile = session.get(Profile, owner)
        records = session.scalars(select(Activity).where(Activity.owner_id == owner)).all()
        plans = session.scalars(select(Plan).where(Plan.owner_id == owner)).all()
        planned = 0
        for plan in plans:
            revision = session.get(PlanRevision, (plan.id, plan.version))
            planned += sum(s["minutes"] for s in revision.payload["sessions"])
        return {
            "activities": sorted(row.minutes for row in records),
            "profile": profile.values if profile else {},
            "plans": len(plans),
            "planned_minutes": planned,
        }


def clean(engine, actors):
    from fitlife.database import DomainRecord, MediaAsset, MediaJob
    from fitlife.media import storage

    owners = [str(a.user_id) for a in actors]
    if engine.url.database != "fitlife_test":
        raise ValueError("TEST_CLEANUP_ONLY")
    for owner in owners:
        folder = storage("test") / owner
        if folder.is_dir():
            for file in folder.glob("*.upload"):
                file.unlink()
            if not any(folder.iterdir()):
                folder.rmdir()
    with Session(engine) as session, session.begin():
        plans = select(Plan.id).where(Plan.owner_id.in_(owners))
        session.execute(delete(PlanRevision).where(PlanRevision.plan_id.in_(plans)))
        for table in (
            DomainRecord,
            MediaJob,
            MediaAsset,
            AccessToken,
            AgentRequest,
            Conversation,
            Plan,
            Activity,
            Profile,
            PendingAction,
        ):
            session.execute(delete(table).where(table.owner_id.in_(owners)))
        session.execute(delete(User).where(User.id.in_(owners)))
    with Session(engine) as session:
        for table in (
            AccessToken,
            AgentRequest,
            Conversation,
            Plan,
            Activity,
            Profile,
            PendingAction,
        ):
            assert session.scalar(select(table).where(table.owner_id.in_(owners))) is None
        assert session.scalar(select(User).where(User.id.in_(owners))) is None


def run_case(case, settings, engine, live, budget):
    result = {"id": case["id"], "group": case["group"], "rules": {}, "turns": [], "run_ids": []}
    actors, models, trace = [], [], []
    service = PlanningService(engine)
    started = None

    def rule(name, condition):
        result["rules"][name] = result["rules"].get(name, True) and bool(condition)

    try:
        alice, token = issue_token(engine)
        actors.append(alice)
        bob, other_token = issue_token(engine)
        actors.append(bob)
        for actor, profile in (
            (alice, case["initial"].get("profile")),
            (bob, case["initial"].get("other_profile")),
        ):
            if profile:
                draft = service.propose(actor, "profile_patch", profile, "setup-profile")
                service.confirm(actor, draft["action_id"], draft["revision"], draft["digest"])
        for i, minutes in enumerate(case["initial"].get("activities", [])):
            draft = service.propose(
                alice,
                "record_activity",
                {"activity_id": "walking", "minutes": minutes},
                f"setup-{i}",
            )
            service.confirm(alice, draft["action_id"], draft["revision"], draft["digest"])
        if case["initial"].get("plan"):
            draft = service.propose_plan(
                alice, PlanRequest.model_validate(case["initial"]["plan"]), "setup-plan"
            )
            service.confirm(alice, draft["action_id"], draft["revision"], draft["digest"])
        records = service.list_activities(alice, today(), today())["records"]
        if records:
            case = json.loads(
                json.dumps(case, ensure_ascii=False).replace("$RECORD_ID", records[0]["id"])
            )
        result["fixture_aliases"] = {
            "actor_id": str(alice.user_id),
            "other_actor_id": str(bob.user_id),
            "first_record_id": records[0]["id"] if records else None,
        }
        other_initial = state(engine, bob)
        fixture = []

        def factory(model_settings):
            model = ResponsesModel(model_settings) if live else FixtureModel(fixture)
            wrapped = budget.wrap(model)
            models.append(wrapped)
            return wrapped

        app = create_app(
            settings,
            engine=engine,
            model_factory=factory,
            synthetic_evaluation=True,
            model_mode="live" if live else "fixed-response",
        )
        previous = None
        with TestClient(app) as client:
            cid = client.post("/conversations", headers=headers(token)).json()["conversation_id"]
            started = time.perf_counter()
            for number, step in enumerate(case["steps"]):
                fixture = step["fixture"]
                key = uuid4()
                before = state(engine, alice)
                body = {"message": step["message"], "conversation_id": cid}
                response = client.post("/agent/requests", headers=headers(token, key), json=body)
                payload = response.json()
                expected_http = 200 if live else step.get("fixture_http", 200)
                rule("http", response.status_code == expected_http)
                rule("no_unconfirmed_business_write", state(engine, alice) == before)
                run_id = payload.get("run_id") or (payload.get("detail") or {}).get("run_id")
                turn_trace = []
                if run_id:
                    result["run_ids"].append(run_id)
                    report = json.loads(
                        (ROOT / "artifacts/runs" / f"{run_id}.json").read_text(encoding="utf-8")
                    )
                    turn_trace = report["trace"]
                    trace.extend(turn_trace)
                calls = [t for t in turn_trace if t["kind"] == "tool"]
                names = [t["name"] for t in calls]
                rule("allowed_tools", set(names) <= set(case["allowed_tools"]))
                required = step.get("required_tools", [])
                rule("tool_selection", set(required) <= set(names))
                if "order" in step:
                    order = step["order"]
                    rule(
                        "tool_order",
                        all(n in names for n in order)
                        and names.index(order[0]) < names.index(order[1]),
                    )
                candidate = payload.get("pending_action")
                if live and "allow_pending_candidate" in step:
                    rule(
                        "candidate_parameters",
                        candidate is None
                        or (
                            candidate.get("status") == "pending"
                            and subset(candidate, step["allow_pending_candidate"])
                        ),
                    )
                elif "candidate" in step:
                    rule("candidate_parameters", subset(candidate, step["candidate"]))
                for expected in [e for e in fixture if e.get("tool") in required and "args" in e]:
                    rule(
                        "tool_parameters",
                        any(
                            c["name"] == expected["tool"]
                            and subset(c.get("arguments"), expected["args"])
                            for c in calls
                        ),
                    )
                answer = payload.get("answer", "")
                rule(
                    "answer_facts",
                    all(s in answer for s in step.get("contains", []))
                    and all(s not in answer for s in step.get("not_contains", [])),
                )
                result["turns"].append(
                    {
                        "input": step["message"],
                        "answer": answer,
                        "http": response.status_code,
                        "end_reason": payload.get("end_reason"),
                    }
                )
                for control in step.get("after", []):
                    if control == "replay":
                        count = len(models)
                        replay = client.post(
                            "/agent/requests", headers=headers(token, key), json=body
                        )
                        rule(
                            "request_replay",
                            replay.status_code == expected_http and len(models) == count,
                        )
                    elif control == "assert_old_invalid":
                        if previous:
                            rejected = client.post(
                                f"/actions/{previous['action_id']}/confirm",
                                headers=headers(token),
                                json=confirmation(previous),
                            )
                            rule("old_confirmation_invalid", rejected.status_code == 409)
                        else:
                            rule("old_confirmation_invalid", False)
                    elif candidate:
                        operation = "reject" if control == "reject" else "confirm"
                        old_state = state(engine, alice)
                        applied = client.post(
                            f"/actions/{candidate['action_id']}/{operation}",
                            headers=headers(token),
                            json=confirmation(candidate),
                        )
                        rule(
                            "confirmation",
                            applied.status_code == 200
                            and applied.json().get("status")
                            == ("rejected" if operation == "reject" else "applied"),
                        )
                        if control == "confirm_again":
                            rule("no_duplicate_write", state(engine, alice) == old_state)
                        cross_user = client.post(
                            f"/actions/{candidate['action_id']}/confirm",
                            headers=headers(other_token),
                            json=confirmation(candidate),
                        )
                        rule("cross_user_blocked", cross_user.status_code == 404)
                    else:
                        rule("confirmation", False)
                previous = candidate or previous
            result["seconds"] = round(time.perf_counter() - started, 3)
        result["final_state"] = state(engine, alice)
        rule("final_database_state", subset(result["final_state"], case["final"]))
        rule("other_user_unchanged", state(engine, bob) == other_initial)
    except Exception as exc:
        result["error_type"] = type(exc).__name__
        rule("execution", False)
    finally:
        if started and "seconds" not in result:
            result["seconds"] = round(time.perf_counter() - started, 3)
        clean(engine, actors)
        result["cleanup"] = "verified"
    result["trace"] = trace
    result["rule_pass"] = all(result["rules"].values())
    result["answer_review"] = "required" if case["review"] else "rule_checked"
    result["model_calls"] = sum(t["kind"] == "model" for t in trace)
    result["tool_calls"] = sum(t["kind"] == "tool" for t in trace)
    result["usage"] = [t.get("usage") for t in trace if t["kind"] == "model"]
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", choices=["dev", "holdout"], default="dev")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--repeat", type=int, choices=range(1, 11), default=1)
    parser.add_argument("--max-model-calls", type=int, default=64)
    parser.add_argument("--case", action="append", default=[])
    args = parser.parse_args()
    raw = DATASET.read_bytes()
    cases = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids))
    groups = {}
    for case in cases:
        assert groups.setdefault(case["group"], case["split"]) == case["split"]
    cases = [
        c
        for c in cases
        if c["split"] == args.split
        and (not args.live or c.get("live", True))
        and (not args.case or c["id"] in args.case)
    ]
    assert cases and args.max_model_calls > 0
    logical_day = today()
    replacements = {
        "$TODAY": logical_day.isoformat(),
        "$WEEK": (logical_day - timedelta(days=logical_day.weekday())).isoformat(),
    }
    resolved = json.dumps(cases, ensure_ascii=False)
    for key, value in replacements.items():
        resolved = resolved.replace(key, value)
    cases = json.loads(resolved)
    settings = DatabaseSettings(environment="test")
    engine = settings.engine()
    budget = ModelBudget(args.max_model_calls)
    results = []
    try:
        migrate(engine, "test")
        for repeat in range(args.repeat):
            for case in cases:
                result = run_case(case, settings, engine, args.live, budget)
                result["repeat"] = repeat + 1
                results.append(result)
                print(
                    f"{args.split}/{case['id']} #{repeat + 1}: "
                    f"{'passed' if result['rule_pass'] else 'failed'}",
                    flush=True,
                )
    finally:
        engine.dispose()
    metric_names = sorted({k for r in results for k in r["rules"]})
    metrics = {}
    for name in metric_names:
        values = [r["rules"][name] for r in results if name in r["rules"]]
        metrics[name] = {"passed": sum(values), "n": len(values), "rate": sum(values) / len(values)}
    latency = [r["seconds"] for r in results if "seconds" in r]
    success = sum(r["rule_pass"] for r in results)
    report = {
        "dataset_version": "core-v1.2",
        "dataset_sha256": hashlib.sha256(raw).hexdigest(),
        "resolved_day_shanghai": str(logical_day),
        "split": args.split,
        "mode": "live" if args.live else "fixed-response-regression",
        "repeat": args.repeat,
        "n_cases": len(cases),
        "n_scripts": len(results),
        "rule_passed": success,
        "rule_pass_rate": success / len(results),
        "metrics": metrics,
        "full_task_success_rate": None,
        "answer_review_required": sum(r["answer_review"] == "required" for r in results),
        "latency_seconds": {
            "n": len(latency),
            "mean": statistics.mean(latency),
            "min": min(latency),
            "max": max(latency),
            "population_stdev": statistics.pstdev(latency),
            "scope": "API script end-to-end; excludes fixtures/setup/cleanup",
        },
        "model_calls": budget.calls,
        "cost": None,
        "cost_status": "provider_price_and_bill_unverified",
        "settings": Settings().model_dump(mode="json"),
        "python": platform.python_version(),
        "packages": {n: version(n) for n in ("openai", "mcp", "fastapi", "SQLAlchemy", "pydantic")},
        "source_sha256": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (ROOT / "fitlife").glob("*.py")
        },
        "cases": results,
    }
    folder = ROOT / "artifacts/evaluations" / str(uuid4())
    folder.mkdir(parents=True)
    report["per_case_repeats"] = {
        case["id"]: {
            "n": len(values := [r for r in results if r["id"] == case["id"]]),
            "rule_passed": sum(r["rule_pass"] for r in values),
            "mean_seconds": statistics.mean(r["seconds"] for r in values),
            "min_seconds": min(r["seconds"] for r in values),
            "max_seconds": max(r["seconds"] for r in values),
            "population_stdev_seconds": statistics.pstdev(r["seconds"] for r in values),
        }
        for case in cases
    }
    with zipfile.ZipFile(folder / "source.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        sources = [
            *(ROOT / "fitlife").glob("*.py"),
            *(ROOT / "fitlife/web").glob("*.html"),
            *(ROOT / "fitlife/web").glob("*.css"),
            *(ROOT / "fitlife/web").glob("*.js"),
            *(ROOT / "migrations").rglob("*.py"),
            DATASET,
            ROOT / "pyproject.toml",
            ROOT / "alembic.ini",
        ]
        for path in sources:
            archive.write(path, str(path.relative_to(ROOT)))
    (folder / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (folder / "resolved-cases.json").write_text(
        json.dumps(cases, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"规则通过 {success}/{len(results)}；完整回答质量需另审；报告：{folder / 'report.json'}")
    if success != len(results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
