"""真实本地模型+注入云端超时的只读MCP/API验收；0云端模型费用。"""

import argparse
import asyncio
import json
import os
import tempfile
import time
from pathlib import Path
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient

from fitlife.api import create_app
from fitlife.auth import issue_token
from fitlife.business import today
from fitlife.database import DatabaseSettings
from fitlife.domains import DomainService
from fitlife.evaluate import clean
from fitlife.execution import Ledger, complete
from fitlife.manage import migrate
from fitlife.model import Settings
from fitlife.model_profiles import configured, resolve

ROOT = Path(__file__).resolve().parents[1]


class UnavailableCloud:
    network_model = True

    def __init__(self, settings):
        self.settings = settings

    async def complete(self, *args):
        await asyncio.sleep(5)
        raise AssertionError("Hard timeout must cancel the injected cloud")

    async def close(self):
        pass


async def protocol_check(settings):
    ledger, trace = Ledger(), []
    model = UnavailableCloud(settings)
    tools = [
        {
            "type": "function",
            "name": "get_activity_catalog",
            "description": "查询应用支持的活动项目。",
            "parameters": {
                "type": "object",
                "properties": {"category": {"type": "string", "enum": ["all"]}},
                "required": ["category"],
                "additionalProperties": False,
            },
        }
    ]
    items = [{"role": "user", "content": "请查询应用支持哪些运动项目。"}]
    prompt = (
        "你是运动助手。先调用get_activity_catalog，category填all，"
        "然后根据真实工具结果用中文回答，不添加其他运动。"
    )
    first = await complete(model, prompt, items, tools, settings, ledger, trace, 1)
    assert len(first.calls) == 1 and first.calls[0]["name"] == "get_activity_catalog"
    assert json.loads(first.calls[0]["arguments"]) == {"category": "all"}
    items.extend(first.continuation)
    items.append(
        {
            "type": "function_call_output",
            "call_id": first.calls[0]["call_id"],
            "output": json.dumps(
                {
                    "activities": [
                        {"id": "walking", "name": "步行"},
                        {"id": "gentle_mobility", "name": "轻柔关节活动"},
                    ]
                },
                ensure_ascii=False,
            ),
        }
    )
    second = await complete(model, prompt, items, tools, settings, ledger, trace, 2)
    assert not second.calls and "步行" in second.text
    assert sum(e["kind"] == "model" and e.get("outcome") == "completed" for e in trace) == 2
    assert ledger.calls == 3
    return {"status": "passed", "attempts": ledger.calls, "trace": trace, "answer": second.text}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-live", action="store_true", required=True)
    parser.parse_args()
    folder = ROOT / "artifacts/model-resilience" / str(uuid4())
    folder.mkdir(parents=True)
    report = {
        "status": "failed",
        "cloud_model_calls": 0,
        "scenario": "injected-cloud-timeout-real-local",
    }
    environment_keys = [
        "FITLIFE_MODEL_PROFILES_FILE",
        "FITLIFE_FALLBACK_PROFILE",
        "FITLIFE_REQUEST_TIMEOUT",
        "FITLIFE_MODEL_RETRIES",
        "FITLIFE_CIRCUIT_FAILURE_THRESHOLD",
        "FITLIFE_ROUTING_POLICY",
        "FITLIFE_ORCHESTRATION",
    ]
    saved = {k: os.environ.get(k) for k in environment_keys}
    db = DatabaseSettings(environment="test")
    engine = db.engine()
    actors = []
    try:
        with httpx.Client(timeout=3, trust_env=False) as client:
            report["ollama_version"] = client.get("http://127.0.0.1:11435/api/version").json()
            report["local_models"] = client.get("http://127.0.0.1:11435/api/tags").json()
        migrate(engine, "test")
        with tempfile.TemporaryDirectory(prefix="fitlife-failover-") as temp:
            p = Path(temp) / "profiles.json"
            profiles = [
                {
                    "name": name,
                    "provider": "injected-cloud",
                    "base_url": "http://127.0.0.1:9/v1",
                    "api_family": "responses",
                    "model": "injected-" + name,
                    "key_env": "UNUSED",
                    "capabilities": ["text", "tools"],
                    "request_timeout": 0.02,
                }
                for name in ("standard", "fast")
            ]
            profiles.append(
                {
                    "name": "local",
                    "provider": "ollama",
                    "base_url": "http://127.0.0.1:11435",
                    "api_family": "ollama",
                    "model": "qwen3:4b-instruct-2507-q4_K_M",
                    "key_env": "UNUSED",
                    "capabilities": ["text", "tools"],
                    "request_timeout": 45,
                }
            )
            p.write_text(json.dumps(profiles), encoding="utf-8")
            os.environ.update(
                FITLIFE_MODEL_PROFILES_FILE=str(p),
                FITLIFE_FALLBACK_PROFILE="local",
                FITLIFE_MODEL_RETRIES="0",
                FITLIFE_CIRCUIT_FAILURE_THRESHOLD="1",
                FITLIFE_ROUTING_POLICY="conservative",
                FITLIFE_ORCHESTRATION="master",
            )
            settings = configured(Settings(), resolve(Settings(), "standard"))
            started = time.perf_counter()
            report["protocol"] = asyncio.run(protocol_check(settings))
            report["protocol_seconds"] = round(time.perf_counter() - started, 3)
            actor, token = issue_token(engine)
            actors.append(actor)
            service = DomainService(engine)
            draft = service.propose_record(actor, "steps", {"steps": 7312}, "local-setup", today())
            service.confirm(actor, draft["action_id"], draft["revision"], draft["digest"])
            before = service.lookup(actor, "steps", today(), today())
            app = create_app(
                db,
                engine=engine,
                model_factory=UnavailableCloud,
                model_mode="local-live-fault-injection",
                orchestration_mode="master",
                synthetic_evaluation=True,
                evaluation_auto_titles=False,
            )
            with TestClient(app) as client:
                key = str(uuid4())
                headers = {"Authorization": "Bearer " + token, "Idempotency-Key": key}
                started = time.perf_counter()
                response = client.post(
                    "/agent/requests",
                    headers=headers,
                    json={"message": "查询今天的步数记录，不要写入数据。"},
                )
                report["api_status"] = response.status_code
                if response.status_code != 200:
                    report["api_error"] = response.json()
                    raise AssertionError("LOCAL_AGENT_TASK_FAILED")
                result = response.json()
                report["api_run_id"] = result["run_id"]
                report["api_response"] = result
                run = json.loads(
                    (ROOT / "artifacts/runs" / (result["run_id"] + ".json")).read_text(
                        encoding="utf-8"
                    )
                )
                (folder / "api-run.json").write_text(
                    json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                assert run["degraded"] and result["end_reason"] == "completed"
                assert "pending_action" not in result
                assert "7312" in result["answer"].replace(",", "").replace("，", "")
                assert any(
                    e.get("name") == "lookup_steps_tool" and e.get("executed") for e in run["trace"]
                )
                replay = client.post(
                    "/agent/requests",
                    headers=headers,
                    json={"message": "查询今天的步数记录，不要写入数据。"},
                )
                assert replay.json()["run_id"] == result["run_id"]
                assert service.lookup(actor, "steps", today(), today()) == before
                report["known_steps_retrieved"] = 7312
                report["business_facts_unchanged"] = True
                report["api_seconds_including_replay"] = round(time.perf_counter() - started, 3)
                report["api_run_id"] = result["run_id"]
                report["api_answer"] = result["answer"]
                report["request_replay_same_run"] = True
            report["status"] = "passed"
    except Exception as exc:
        report["error_type"] = type(exc).__name__
        # Never dump native/provider exception bodies or credentials.
    finally:
        clean(engine, actors)
        report["scoped_fixture_cleanup"] = True
        engine.dispose()
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        (folder / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(f"本地故障切换验收：{report['status']}；报告：{folder / 'report.json'}")
    if report["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
