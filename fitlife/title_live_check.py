"""本机硅基流动免费8B：真实首轮命名、用户覆盖及幂等重放。"""

import argparse
import json
import os
import tempfile
import threading
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from fitlife.api import create_app
from fitlife.auth import issue_token
from fitlife.business import today
from fitlife.database import DatabaseSettings
from fitlife.domains import DomainService
from fitlife.evaluate import clean
from fitlife.manage import migrate
from fitlife.model import create_model
from fitlife.siliconflow_check import public_prices

ROOT = Path(__file__).resolve().parents[1]
MODEL = "Qwen/Qwen3-8B"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True)
    parser.parse_args()
    folder = ROOT / "artifacts/conversation-titles" / str(uuid4())
    folder.mkdir(parents=True)
    report = {"status": "failed", "model": MODEL, "paid_models_allowed": False}
    db = DatabaseSettings(environment="test")
    engine, actors, calls = db.engine(), [], []
    lock = threading.Lock()
    keys = (
        "FITLIFE_MODEL_PROFILES_FILE",
        "FITLIFE_ROUTING_POLICY",
        "FITLIFE_ROUTER_MODEL",
        "FITLIFE_EASY_MODEL",
        "FITLIFE_MODEL_RETRIES",
        "FITLIFE_FALLBACK_PROFILE",
    )
    saved = {k: os.environ.get(k) for k in keys}
    try:
        price = public_prices()[MODEL]
        assert all(float(v) == 0 for v in price["rates_cny_per_million"].values())
        report["price_snapshot"] = price
        migrate(engine, "test")
        with tempfile.TemporaryDirectory(prefix="fitlife-title-model-") as temp:
            path = Path(temp) / "profiles.json"
            path.write_text(
                json.dumps(
                    [
                        {
                            "name": name,
                            "provider": "siliconflow",
                            "base_url": "https://api.siliconflow.cn/v1",
                            "api_family": "chat",
                            "model": MODEL,
                            "key_env": "SILI_API_KEY",
                            "capabilities": ["text", "tools"],
                            "enable_thinking": False,
                            "request_timeout": 30,
                        }
                        for name in ("standard", "fast")
                    ]
                ),
                encoding="utf-8",
            )
            os.environ.update(dict(zip(keys, (str(path), "fixed", "fast", "fast", "0", ""))))

            def factory(settings):
                assert (
                    settings.model == MODEL
                    and settings.base_url.rstrip("/") == "https://api.siliconflow.cn/v1"
                )
                model = create_model(settings)

                async def record(request):
                    data = json.loads(request.content)
                    assert data["model"] == MODEL and not data.get("models")
                    with lock:
                        assert len(calls) < 10, "TITLE_CHECK_CALL_BUDGET_EXHAUSTED"
                        calls.append({"model": MODEL, "max_tokens": data.get("max_tokens")})

                model.client._client.event_hooks = {"request": [record]}
                return model

            actor, token = issue_token(engine)
            actors.append(actor)
            service = DomainService(engine)
            draft = service.propose_record(actor, "steps", {"steps": 5634}, "title-setup", today())
            service.confirm(actor, draft["action_id"], draft["revision"], draft["digest"])
            before = service.lookup(actor, "steps", today(), today())
            with TestClient(
                create_app(
                    db,
                    engine=engine,
                    model_factory=factory,
                    orchestration_mode="master",
                    model_mode="siliconflow-title-live",
                )
            ) as client:
                auth = {"Authorization": "Bearer " + token}
                cid = client.post("/conversations", headers=auth).json()["conversation_id"]
                key = str(uuid4())
                headers = {**auth, "Idempotency-Key": key}
                body = {"conversation_id": cid, "message": "查询今天的步数记录，不要修改数据。"}
                response = client.post("/agent/requests", headers=headers, json=body)
                report["api_status"] = response.status_code
                assert response.status_code == 200
                report["run_id"] = response.json()["run_id"]
                assert "5634" in response.json()["answer"].replace(",", "")
                detail = client.get(f"/conversations/{cid}", headers=auth).json()
                report["generated_name"] = detail["title"]
                assert detail["title_source"] == "agent" and detail["title"] != "新对话"
                renamed = client.patch(
                    f"/conversations/{cid}/title",
                    headers=auth,
                    json={"title": "我的步行回顾", "revision": detail["title_revision"]},
                )
                assert renamed.status_code == 200
                count = len(calls)
                replay = client.post("/agent/requests", headers=headers, json=body)
                assert replay.json()["run_id"] == report["run_id"] and len(calls) == count
                detail = client.get(f"/conversations/{cid}", headers=auth).json()
                assert detail["title"] == "我的步行回顾" and detail["title_source"] == "user"
                assert service.lookup(actor, "steps", today(), today()) == before
                report.update(
                    status="passed",
                    manual_name_preserved_on_replay=True,
                    replay_model_calls=0,
                    business_facts_unchanged=True,
                )
    except Exception as exc:
        report["error_type"] = type(exc).__name__
    finally:
        clean(engine, actors)
        report["scoped_fixture_cleanup"] = True
        report["model_calls"] = calls
        engine.dispose()
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        (folder / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(f"真实会话命名验收：{report['status']}；报告：{folder / 'report.json'}")
    if report["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
