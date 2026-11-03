import pytest

from fitlife import api_check, db_check, plan_check, session_check

pytestmark = pytest.mark.integration


def test_business_transactions_permissions_and_confirmation(test_database):
    _, engine = test_database
    checks = []
    db_check.verify(engine, checks)
    assert len(checks) >= 9


@pytest.mark.parametrize(
    "module,function",
    [
        (api_check, "check"),
        (plan_check, "verify"),
        (session_check, "verify"),
    ],
)
def test_api_plan_and_conversation_regressions(test_database, module, function):
    settings, engine = test_database
    report = {"checks": []}
    getattr(module, function)(engine, settings, report, False)
    assert report["checks"] and report["fixture_cleanup"] == "verified"
