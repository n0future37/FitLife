"""本人会话管理。临时聊天与已确认画像分开，删除聊天不删除运动事实。"""

from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from fitlife.business import BusinessError, utcnow
from fitlife.database import AgentRequest, Conversation, PendingAction, User


def owned(session, actor, conversation_id):
    row = session.get(Conversation, str(conversation_id))
    if row is None or row.owner_id != str(actor.user_id) or row.deleted:
        raise BusinessError("CONVERSATION_NOT_FOUND")
    return row


def create(engine, actor):
    with Session(engine) as session, session.begin():
        if session.get(User, str(actor.user_id), with_for_update=True) is None:
            raise BusinessError("USER_NOT_FOUND")
        row = Conversation(
            id=str(uuid4()),
            owner_id=str(actor.user_id),
            created_at=utcnow(),
            updated_at=utcnow(),
            lease_until=utcnow(),
            deleted=False,
        )
        session.add(row)
        return {"conversation_id": row.id}


def recent(engine, actor):
    with Session(engine) as session:
        rows = session.scalars(
            select(Conversation)
            .where(
                Conversation.owner_id == str(actor.user_id),
                Conversation.deleted.is_(False),
            )
            .order_by(Conversation.updated_at.desc(), Conversation.id)
            .limit(50)
        )
        return {
            "conversations": [
                {"conversation_id": row.id, "updated_at": row.updated_at.isoformat() + "Z"}
                for row in rows
            ],
            "limit": 50,
        }


def transcript(engine, actor, conversation_id):
    with Session(engine) as session:
        conversation = owned(session, actor, conversation_id)
        rows = list(
            session.scalars(
                select(AgentRequest)
                .where(
                    AgentRequest.conversation_id == conversation.id,
                    AgentRequest.owner_id == str(actor.user_id),
                )
                .order_by(AgentRequest.turn_index.desc())
                .limit(50)
            )
        )
        messages = []
        for row in reversed(rows):
            response = dict(row.response or {})
            candidate = response.get("pending_action")
            if candidate:
                action = session.get(PendingAction, candidate["action_id"])
                if action and action.owner_id == row.owner_id:
                    from fitlife.business import snapshot

                    response["pending_action"] = snapshot(action)
            messages.append(
                {
                    "request_key": row.request_key,
                    "message": row.user_message,
                    "status": row.status,
                    "response": response,
                }
            )
        return {"conversation_id": conversation.id, "turns": messages, "limit": 50}


def erase(engine, actor, conversation_id):
    with Session(engine) as session, session.begin():
        session.get(User, str(actor.user_id), with_for_update=True)
        conversation = owned(session, actor, conversation_id)
        if conversation.active_request_key and conversation.lease_until > utcnow():
            raise BusinessError("CONVERSATION_IN_PROGRESS")
        rows = session.scalars(
            select(AgentRequest).where(
                AgentRequest.conversation_id == conversation.id,
                AgentRequest.owner_id == str(actor.user_id),
            )
        )
        for row in rows:
            # 保留幂等墓碑，清除聊天正文/上下文/缓存。旧请求不能被复活。
            row.user_message, row.context, row.response = None, None, None
            row.status, row.nonce = "erased", str(uuid4())
            action = session.scalar(
                select(PendingAction).where(
                    PendingAction.owner_id == row.owner_id,
                    PendingAction.request_key == "agent:" + row.request_key,
                    PendingAction.status == "pending",
                )
            )
            if action:
                action.status = "superseded"
        conversation.deleted, conversation.active_request_key = True, None
        return {"status": "erased", "business_data_retained": True}


def history(session, owner, conversation_id, memory_version, memory_scope=None):
    """最多6个完整成功回合、12000字符；不使用运行失败的回答推断业务成功。"""
    rows = list(
        session.scalars(
            select(AgentRequest)
            .where(
                AgentRequest.owner_id == owner,
                AgentRequest.conversation_id == conversation_id,
                AgentRequest.status == "completed",
                AgentRequest.memory_version == memory_version,
                AgentRequest.memory_scope == memory_scope if memory_scope is not None else True,
            )
            .order_by(AgentRequest.turn_index.desc())
            .limit(6)
        )
    )
    items, used = [], 0
    for row in rows:
        answer = (row.response or {}).get("answer", "")
        size = len(row.user_message or "") + len(answer)
        if used + size > 12000:
            break
        used += size
        items[0:0] = [
            {"role": "user", "content": row.user_message or ""},
            {"role": "assistant", "content": answer},
        ]
    return items
