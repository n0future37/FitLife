import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from fitlife import conversations, requests
from fitlife.api import create_app
from fitlife.auth import issue_token
from fitlife.business import BusinessError
from fitlife.conversation_titles import summarize
from fitlife.evaluate import clean
from fitlife.execution import Ledger
from fitlife.model import Reply, Settings


def test_title_validation_handles_blank_length_and_invisible_lines():
    assert conversations.normalize_title("  本周步行  ") == "本周步行"
    for title in ("", "  ", "a" * 41, "a\nb", "a\u2028b", "a\0b"):
        with pytest.raises(BusinessError, match="INVALID_CONVERSATION_TITLE"):
            conversations.normalize_title(title)


@pytest.mark.integration
def test_user_rename_wins_over_late_agent_and_is_owner_version_bound(test_database):
    _, engine = test_database
    actor, _ = issue_token(engine)
    other, _ = issue_token(engine)
    try:
        cid = conversations.create(engine, actor)["conversation_id"]
        claim = requests.claim(engine, actor, uuid4(), "首次请求", 30, cid)
        assert claim["title_seed"] == ()
        renamed = conversations.rename(engine, actor, cid, "我的训练安排", 0)
        requests.finish(
            engine, actor, claim["id"], claim["nonce"], {"answer": "ok"}, True, "模型标题"
        )
        detail = conversations.transcript(engine, actor, cid)
        assert detail["title"] == "我的训练安排" and detail["title_source"] == "user"
        assert detail["title_revision"] == renamed["title_revision"] == 1
        with pytest.raises(BusinessError, match="STALE_CONVERSATION_TITLE"):
            conversations.rename(engine, actor, cid, "旧版本", 0)
        with pytest.raises(BusinessError, match="CONVERSATION_NOT_FOUND"):
            conversations.rename(engine, other, cid, "越权", 1)
        conversations.erase(engine, actor, cid)
        from sqlalchemy.orm import Session

        from fitlife.database import Conversation

        with Session(engine) as session:
            assert session.get(Conversation, cid).title == "新对话"
    finally:
        clean(engine, [actor, other])


@pytest.mark.integration
def test_first_success_seed_is_preserved_and_cache_does_not_rename(test_database):
    _, engine = test_database
    actor, _ = issue_token(engine)
    try:
        cid = conversations.create(engine, actor)["conversation_id"]
        first = requests.claim(engine, actor, uuid4(), "失败消息", 30, cid)
        requests.finish(engine, actor, first["id"], first["nonce"], {}, False)
        key = uuid4()
        second = requests.claim(engine, actor, key, "第一次成功的话题", 30, cid)
        requests.finish(engine, actor, second["id"], second["nonce"], {"answer": "首次回答"}, True)
        third = requests.claim(engine, actor, uuid4(), "后来的话题", 30, cid)
        assert third["title_seed"] == ("第一次成功的话题", "首次回答")
        requests.finish(
            engine, actor, third["id"], third["nonce"], {"answer": "ok"}, True, "首次话题摘要"
        )
        assert conversations.recent(engine, actor)["conversations"][0]["title_source"] == "agent"
        assert requests.claim(engine, actor, key, "第一次成功的话题", 30, cid) == {
            "cached": {"answer": "首次回答"}
        }
        next_ = requests.claim(engine, actor, uuid4(), "新一轮", 30, cid)
        assert next_["title_seed"] is None
        requests.finish(engine, actor, next_["id"], next_["nonce"], {}, True, "不能覆盖")
        assert conversations.transcript(engine, actor, cid)["title"] == "首次话题摘要"
    finally:
        clean(engine, [actor])


@pytest.mark.integration
def test_title_api_validates_owner_stale_version_and_text(test_database):
    db, engine = test_database
    actor, token = issue_token(engine)
    other, other_token = issue_token(engine)
    try:
        with TestClient(create_app(db, engine=engine)) as client:
            headers = {"Authorization": "Bearer " + token}
            cid = client.post("/conversations", headers=headers).json()["conversation_id"]
            url = f"/conversations/{cid}/title"
            assert (
                client.patch(
                    url,
                    headers={"Authorization": "Bearer " + other_token},
                    json={"title": "x", "revision": 0},
                ).status_code
                == 404
            )
            assert (
                client.patch(
                    url, headers=headers, json={"title": "a\nb", "revision": 0}
                ).status_code
                == 422
            )
            title = "<b>我的运动</b>"
            response = client.patch(url, headers=headers, json={"title": title, "revision": 0})
            assert response.status_code == 200 and response.json()["title"] == title
            assert response.json()["title_source"] == "user"
            assert (
                client.patch(
                    url, headers=headers, json={"title": "过期", "revision": 0}
                ).status_code
                == 409
            )
            assert client.get(f"/conversations/{cid}", headers=headers).json()["title"] == title
    finally:
        clean(engine, [actor, other])


@pytest.mark.asyncio
async def test_title_model_respects_shared_budget_and_failure_is_optional():
    calls = []

    class Model:
        supports_titles = True

        def __init__(self, settings):
            self.settings = settings

        async def complete(self, prompt, items, tools):
            calls.append((prompt, items, tools))
            return Reply(text='{"title":"本周运动计划"}')

        async def close(self):
            pass

    settings = Settings(max_model_calls=1)
    ledger, trace = Ledger(), []
    assert await summarize(("安排运动", "待确认"), settings, ledger, trace, Model) == "本周运动计划"
    assert ledger.calls == 1 and len(calls) == 1 and calls[0][2] == []
    assert await summarize(("不要再调用", ""), settings, ledger, trace, Model) is None
    assert len(calls) == 1
    assert all(e.get("stage") == "conversation_title" for e in trace)


@pytest.mark.asyncio
async def test_run_returns_title_but_does_not_log_private_title(tmp_path, monkeypatch):
    import fitlife.agent as agent

    async def execute(*args, **kwargs):
        return {"answer": "草案尚未保存", "end_reason": "pending_confirmation", "steps": 0}

    class Model:
        supports_titles = True

        def __init__(self, settings):
            self.settings = settings

        async def complete(self, prompt, items, tools):
            return Reply(text='{"title":"我的步行计划"}')

        async def close(self):
            pass

    monkeypatch.setattr(agent, "execute", execute)
    monkeypatch.setattr(agent, "PROJECT_ROOT", tmp_path)
    result = await agent.run(
        "制定步行计划", context=agent.RuntimeContext(title_seed=()), model_factory=Model
    )
    assert result["conversation_title"] == "我的步行计划"
    assert result["end_reason"] == "pending_confirmation"
    saved = json.loads(open(result["report_path"], encoding="utf-8").read())
    assert "conversation_title" not in saved and "我的步行计划" not in json.dumps(
        saved, ensure_ascii=False
    )
