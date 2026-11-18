"""高熵随机个人令牌；仅存SHA256摘要。不是密码注册或OAuth服务。"""

import hashlib
import secrets
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from fitlife.business import Actor, BusinessError, utcnow
from fitlife.database import AccessToken, User


def digest_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


def issue_token(engine, user_id=None, hours=24):
    """仅可信本机管理入口/验收调用；原文仅返回一次，不写文件或日志。"""
    if not 1 <= hours <= 720:
        raise ValueError("INVALID_TOKEN_LIFETIME")
    owner = user_id or uuid4()
    token = secrets.token_urlsafe(32)
    with Session(engine) as session, session.begin():
        if user_id is None:
            session.add(User(id=str(owner)))
            session.flush()
        elif session.get(User, str(owner)) is None:
            raise BusinessError("USER_NOT_FOUND")
        session.add(
            AccessToken(
                digest=digest_token(token),
                owner_id=str(owner),
                expires_at=utcnow() + timedelta(hours=hours),
                revoked=False,
            )
        )
    return Actor(UUID(str(owner))), token


def authenticate(engine, token):
    if not isinstance(token, str) or len(token) != 43:
        raise BusinessError("UNAUTHENTICATED")
    with Session(engine) as session:
        credential = session.scalar(
            select(AccessToken)
            .join(User, User.id == AccessToken.owner_id)
            .where(AccessToken.digest == digest_token(token))
        )
        if credential is None or credential.revoked or credential.expires_at <= utcnow():
            raise BusinessError("UNAUTHENTICATED")
        return Actor(UUID(credential.owner_id))


def revoke_token(engine, token):
    with Session(engine) as session, session.begin():
        credential = session.get(AccessToken, digest_token(token), with_for_update=True)
        if credential is not None:
            credential.revoked = True
