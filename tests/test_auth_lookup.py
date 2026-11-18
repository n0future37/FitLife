from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from fitlife.auth import authenticate, digest_token, revoke_token
from fitlife.business import BusinessError, utcnow
from fitlife.database import AccessToken, User


@pytest.fixture
def credentials():
    # Only the portable authentication tables; actual MySQL is checked separately.
    engine = create_engine("sqlite://")
    User.__table__.create(engine)
    AccessToken.__table__.create(engine)
    owner, token = str(uuid4()), "x" * 43
    with Session(engine) as session, session.begin():
        session.add(User(id=owner))
        session.add(
            AccessToken(
                digest=digest_token(token),
                owner_id=owner,
                expires_at=utcnow() + timedelta(hours=1),
                revoked=False,
            )
        )
    yield engine, owner, token
    engine.dispose()


def test_valid_identity_and_revocation_are_not_cached(credentials):
    engine, owner, token = credentials
    assert str(authenticate(engine, token).user_id) == owner
    revoke_token(engine, token)
    with pytest.raises(BusinessError, match="UNAUTHENTICATED"):
        authenticate(engine, token)


@pytest.mark.parametrize("state", ["expired", "missing_owner", "unknown_token"])
def test_untrusted_credentials_remain_rejected(credentials, state):
    engine, owner, token = credentials
    with Session(engine) as session, session.begin():
        if state == "expired":
            session.get(AccessToken, digest_token(token)).expires_at = utcnow() - timedelta(
                seconds=1
            )
        elif state == "missing_owner":
            session.delete(session.get(User, owner))
        else:
            token = "y" * 43
    with pytest.raises(BusinessError, match="UNAUTHENTICATED"):
        authenticate(engine, token)


@pytest.mark.parametrize("token", [None, "short", 123])
def test_malformed_credentials_remain_rejected(credentials, token):
    with pytest.raises(BusinessError, match="UNAUTHENTICATED"):
        authenticate(credentials[0], token)
