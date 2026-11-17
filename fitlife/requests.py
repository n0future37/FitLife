"""持久化Agent请求身份与完成结果；不保存原始用户消息。"""

import hashlib
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from fitlife.business import BusinessError, today, utcnow
from fitlife.constraints import memory_scope
from fitlife.conversations import history, normalize_title, owned, title_seed
from fitlife.database import AgentRequest, PendingAction, Profile, User


def claim(engine, actor, key, message, lease_seconds, conversation_id=None):
    with Session(engine) as session, session.begin():
        owner = session.get(User, str(actor.user_id), with_for_update=True)
        if owner is None:
            raise BusinessError("USER_NOT_FOUND")
        conversation = owned(session, actor, conversation_id) if conversation_id else None
        profile = session.get(Profile, owner.id)
        memory_version = profile.version if profile else 0
        scope = memory_scope(memory_version, profile.values if profile else {}, today())
        row = session.scalar(
            select(AgentRequest).where(
                AgentRequest.owner_id == owner.id,
                AgentRequest.request_key == str(key),
            )
        )
        message_hash = hashlib.sha256(message.encode()).hexdigest()
        if row:
            if row.message_hash != message_hash or row.conversation_id != (
                str(conversation_id) if conversation_id else None
            ):
                raise BusinessError("IDEMPOTENCY_CONFLICT")
            if row.status == "erased":
                raise BusinessError("REQUEST_ERASED")
            if row.status == "completed":
                return {"cached": row.response}
            if row.status == "running" and row.lease_until > utcnow():
                raise BusinessError("REQUEST_IN_PROGRESS")
            if conversation and (row.memory_version != memory_version or row.memory_scope != scope):
                raise BusinessError("REQUEST_CONTEXT_STALE")
        else:
            if conversation:
                if conversation.active_request_key and conversation.lease_until > utcnow():
                    raise BusinessError("CONVERSATION_IN_PROGRESS")
                # 新请求立即撤销旧待确认，避免修改要求后旧卡片仍可确认。
                old_requests = select(AgentRequest.request_key).where(
                    AgentRequest.conversation_id == conversation.id,
                    AgentRequest.owner_id == owner.id,
                )
                old_rows = session.scalars(
                    select(PendingAction).where(
                        PendingAction.owner_id == owner.id,
                        PendingAction.status == "pending",
                        PendingAction.request_key.in_(
                            select(("agent:" + AgentRequest.request_key)).where(
                                AgentRequest.request_key.in_(old_requests),
                                AgentRequest.owner_id == owner.id,
                            )
                        ),
                    )
                )
                for action in old_rows:
                    action.status = "superseded"
                conversation.version += 1
            row = AgentRequest(
                id=str(uuid4()),
                owner_id=owner.id,
                request_key=str(key),
                message_hash=message_hash,
                created_at=utcnow(),
                conversation_id=conversation.id if conversation else None,
                user_message=message if conversation else None,
                context=history(session, owner.id, conversation.id, memory_version, scope)
                if conversation
                else None,
                turn_index=conversation.version if conversation else None,
                memory_version=memory_version if conversation else None,
                memory_scope=scope if conversation else None,
            )
            session.add(row)
        row.status, row.nonce = "running", str(uuid4())
        row.lease_until = utcnow() + timedelta(seconds=lease_seconds)
        if conversation:
            if conversation.active_request_key not in {None, str(key)}:
                if conversation.lease_until > utcnow() or row.turn_index != conversation.version:
                    raise BusinessError("REQUEST_SUPERSEDED")
            if row.turn_index != conversation.version:
                raise BusinessError("REQUEST_SUPERSEDED")
            conversation.active_request_key = str(key)
            conversation.lease_until = row.lease_until
            conversation.updated_at = utcnow()
        return {
            "id": row.id,
            "nonce": row.nonce,
            "created_at": row.created_at,
            "history": row.context or [],
            "title_seed": title_seed(session, actor, conversation) if conversation else None,
        }


def finish(engine, actor, request_id, nonce, response, successful, generated_title=None):
    with Session(engine) as session, session.begin():
        session.get(User, str(actor.user_id), with_for_update=True)
        row = session.get(AgentRequest, request_id, with_for_update=True)
        if row is None or row.owner_id != str(actor.user_id) or row.nonce != nonce:
            raise BusinessError("REQUEST_SUPERSEDED")
        if row.conversation_id:
            conversation = owned(session, actor, row.conversation_id)
            if conversation.active_request_key != row.request_key:
                raise BusinessError("REQUEST_SUPERSEDED")
            conversation.active_request_key = None
            conversation.updated_at = utcnow()
            if successful and generated_title and conversation.title_source == "pending":
                conversation.title = normalize_title(generated_title)
                conversation.title_source = "agent"
                conversation.title_revision += 1
        row.status = "completed" if successful else "failed"
        row.response = response


def recover_candidate(engine, actor, key):
    from fitlife.business import snapshot

    with Session(engine) as session:
        row = session.scalar(
            select(PendingAction).where(
                PendingAction.owner_id == str(actor.user_id),
                PendingAction.request_key == "agent:" + str(key),
            )
        )
        return snapshot(row) if row else None
