"""Two real API queries using the shipped SF stack; shared repair budget, no titles."""

import argparse
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from fitlife.api import create_app
from fitlife.api_check import headers
from fitlife.auth import issue_token
from fitlife.business import today
from fitlife.database import DatabaseSettings, User
from fitlife.domains import DomainService
from fitlife.evaluate import clean
from fitlife.evaluate_multi import facts
from fitlife.experiment_budget import ExperimentBudget
from fitlife.model import create_model

ROOT = Path(__file__).resolve().parents[1]


def main(campaign):
    protocol = json.loads((campaign / "protocol.json").read_text(encoding="utf-8"))
    profiles = ROOT / "config/siliconflow.models.json"
    os.environ.update(
        {
            "FITLIFE_MODEL_PROFILES_FILE": str(profiles),
            "FITLIFE_ROUTING_POLICY": "conservative",
            "FITLIFE_ROUTER_MODEL": "control",
            "FITLIFE_PLANNER_MODEL": "control",
            "FITLIFE_SYNTHESIS_MODEL": "control",
            "FITLIFE_EASY_MODEL": "fast",
            "FITLIFE_MODEL_RETRIES": "1",
            "FITLIFE_FALLBACK_PROFILE": "local",
            "FITLIFE_MAX_OUTPUT_TOKENS": "1024",
            "FITLIFE_MASTER_RUN_TIMEOUT": "180",
        }
    )
    settings = DatabaseSettings(environment="test")
    engine = settings.engine()
    actor, token = issue_token(engine)
    folder = ROOT / "artifacts/agent-repairs-v2" / ("production-" + str(uuid4()))
    folder.mkdir(parents=True)
    (folder / "fixture-actors.json").write_text(json.dumps([str(actor.user_id)]), encoding="utf-8")
    budget = ExperimentBudget(
        Path(protocol["budget_file"]),
        protocol["prices"],
        limit=protocol["budget_limit_cny"],
        tag=folder.name,
        response_dir=folder / "model-replies",
    )
    report = {
        "status": "failed",
        "model_retries": 1,
        "run_timeout": 180,
        "results": [],
        "profile_sha256": hashlib.sha256(profiles.read_bytes()).hexdigest(),
        "source_sha256": {
            p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((ROOT / "fitlife").glob("*.py"))
        },
        "mode": "live production stack in synthetic test app; title disabled",
    }
    try:
        service = DomainService(engine)
        for kind, value in (("body", {"weight_kg": 64.9}), ("steps", {"steps": 7531})):
            candidate = service.propose_record(
                actor, kind, {**value, "record_date": str(today())}, str(uuid4()), today()
            )
            service.confirm(
                actor, candidate["action_id"], candidate["revision"], candidate["digest"]
            )
        before = facts(engine, actor)

        def model_factory(model_settings):
            # Local fallback has no cloud charge; it remains visible in the run trace.
            return (
                create_model(model_settings)
                if model_settings.provider == "ollama"
                else budget.factory(model_settings)
            )

        with TestClient(
            create_app(
                settings,
                engine=engine,
                model_factory=model_factory,
                orchestration_mode="master",
                synthetic_evaluation=True,
                evaluation_auto_titles=False,
            )
        ) as client:
            for message, anchor, expected_models, expected_domains in (
                (
                    f"先查询{today()}的体重，再结合训练档案说明运动安排还缺哪些信息。",
                    "64.9",
                    {"Pro/deepseek-ai/DeepSeek-V3.2"},
                    {"body", "exercise"},
                ),
                (f"只查询{today()}的步数，不做建议。", "7531", {"Qwen/Qwen3-8B"}, {"exercise"}),
            ):
                key = uuid4()
                response = client.post(
                    "/agent/requests", headers=headers(token, key), json={"message": message}
                )
                data = response.json()
                rid = data.get("run_id") or data.get("detail", {}).get("run_id")
                raw = (
                    json.loads(
                        (ROOT / "artifacts/runs" / (rid + ".json")).read_text(encoding="utf-8")
                    )
                    if rid
                    else {}
                )
                if rid:
                    (folder / (rid + ".json")).write_text(
                        json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                selected = {
                    e["selection"]["requested_model"]
                    for e in raw.get("trace", [])
                    if e.get("kind") == "delegate_agent"
                }
                delegates = [e for e in raw.get("trace", []) if e.get("kind") == "delegate_agent"]
                checks = {
                    "completed": response.status_code == 200
                    and data.get("end_reason") == "completed",
                    "answer_anchor": anchor
                    in data.get("answer", "").replace(",", "").replace("，", ""),
                    "expected_child_models": selected == expected_models,
                    "expected_domains": {e["agent"] for e in delegates} == expected_domains,
                    "facts_unchanged": facts(engine, actor) == before,
                    "ledger_steps_present": raw.get("steps", 0) > 0
                    and raw.get("execution_stats_version") == "ledger-v2",
                }
                if expected_domains == {"body", "exercise"}:
                    by_domain = {e["agent"]: e for e in delegates}
                    checks["dependency_before_child"] = (
                        "body" in by_domain
                        and "exercise" in by_domain
                        and "body" in by_domain["exercise"]["depends_on"]
                        and by_domain["body"]["finished_ns"] <= by_domain["exercise"]["started_ns"]
                    )
                if response.status_code == 200:
                    calls = budget.local_calls
                    replay = client.post(
                        "/agent/requests", headers=headers(token, key), json={"message": message}
                    )
                    checks["replay_no_model"] = (
                        replay.status_code == 200
                        and replay.json().get("run_id") == rid
                        and budget.local_calls == calls
                    )
                report["results"].append(
                    {"message": message, "response": data, "run_id": rid, "checks": checks}
                )
        report["status"] = (
            "passed" if all(all(r["checks"].values()) for r in report["results"]) else "failed"
        )
    finally:
        clean(engine, [actor])
        with Session(engine) as session:
            report["cleanup_verified"] = session.get(User, str(actor.user_id)) is None
        engine.dispose()
        report["budget_events"] = [
            e for e in budget.read()["attempts"] if e.get("episode") == folder.name
        ]
        (folder / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print("PRODUCTION_CHECK " + str(folder / "report.json"))
    if report["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("campaign", type=Path)
    parser.add_argument("--live", action="store_true", required=True)
    args = parser.parse_args()
    main(args.campaign.resolve())
