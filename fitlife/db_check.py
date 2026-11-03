"""测试库迁移与业务验收；密码隐藏输入，只清理本次随机用户的数据。"""

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Barrier
from uuid import uuid4

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import delete, event, inspect, select, text
from sqlalchemy.orm import Session

from fitlife.business import Actor, BusinessError, BusinessService, today, utcnow
from fitlife.credentials import database_password
from fitlife.database import (
    Activity,
    Base,
    DatabaseSettings,
    PendingAction,
    Profile,
    User,
)

ROOT = Path(__file__).resolve().parents[1]


def migration_config():
    return Config(str(ROOT / "alembic.ini"))


def expect_error(code, callback):
    try:
        callback()
    except BusinessError as exc:
        assert exc.code == code, f"Expected {code}, got {exc.code}"
    else:
        raise AssertionError(f"Expected {code}")


def apply(service, actor, candidate):
    return service.confirm(
        actor, candidate["action_id"], candidate["revision"], candidate["digest"]
    )


def verify(engine, checks):
    owners = [uuid4(), uuid4()]
    alice, bob = (Actor(owner) for owner in owners)
    service = BusinessService(engine)
    try:
        with Session(engine) as session, session.begin():
            session.add_all([User(id=str(owner)) for owner in owners])
        assert service.get_profile(alice)["version"] == 0
        proposal = service.propose(
            alice,
            "profile_patch",
            {
                "goal": "建立规律运动习惯",
                "preferred_activities": ["walking"],
                "available_weekdays": [0, 2, 4],
                "weekly_sessions": 3,
                "session_minutes": 20,
            },
            "profile-1",
        )
        assert service.get_profile(alice)["version"] == 0
        apply(service, alice, proposal)
        profile = service.get_profile(alice)
        assert profile["version"] == 1 and profile["values"]["session_minutes"] == 20
        assert profile["provenance"]["goal"]["action_id"] == proposal["action_id"]
        checks.append("profile_confirmation_and_provenance")
        expect_error("ACTION_NOT_FOUND", lambda: service.get_action(bob, proposal["action_id"]))
        expect_error("ACTION_NOT_FOUND", lambda: apply(service, bob, proposal))
        assert service.get_profile(bob)["values"] == {}
        checks.append("owner_isolation")

        payload = {
            "activity_id": "walking",
            "occurred_on": today().isoformat(),
            "minutes": 20,
        }
        pending = service.propose(alice, "record_activity", payload, "activity-1")
        repeated = service.propose(alice, "record_activity", payload, "activity-1")
        assert pending["action_id"] == repeated["action_id"]
        expect_error(
            "IDEMPOTENCY_CONFLICT",
            lambda: service.propose(
                alice, "record_activity", {**payload, "minutes": 30}, "activity-1"
            ),
        )
        assert service.list_activities(alice, today(), today())["record_count"] == 0
        checks.append("proposal_retry_without_business_write")
        revised = service.revise(
            alice,
            pending["action_id"],
            pending["revision"],
            pending["digest"],
            {**payload, "minutes": 25},
        )
        expect_error("STALE_CONFIRMATION", lambda: apply(service, alice, pending))
        saved = apply(service, alice, revised)
        # 新服务对象代表恢复后重新确认，不再次创建活动。
        assert apply(BusinessService(engine), alice, revised)["result"] == saved["result"]
        rows = service.list_activities(alice, today(), today())
        assert rows["record_count"] == 1 and rows["recorded_minutes"] == 25
        assert service.list_activities(bob, today(), today())["record_count"] == 0
        checks.append("revision_binding_duplicate_confirm_and_resume")

        rejected = service.propose(alice, "record_activity", payload, "activity-reject")
        service.reject(alice, rejected["action_id"], rejected["revision"], rejected["digest"])
        expect_error("ACTION_NOT_PENDING", lambda: apply(service, alice, rejected))
        checks.append("rejection_blocks_write")
        expired = service.propose(alice, "record_activity", payload, "activity-expired")
        with Session(engine) as session, session.begin():
            session.get(PendingAction, expired["action_id"]).expires_at = utcnow() - timedelta(
                seconds=1
            )
        assert apply(service, alice, expired)["status"] == "expired"
        checks.append("expiry_blocks_write")

        first = service.propose(alice, "profile_patch", {"session_minutes": 15}, "profile-2")
        stale = service.propose(alice, "profile_patch", {"session_minutes": 30}, "profile-3")
        apply(service, alice, first)
        assert apply(service, alice, stale)["status"] == "superseded"
        assert service.get_profile(alice)["values"]["session_minutes"] == 15
        checks.append("profile_version_conflict")

        fault = service.propose(alice, "record_activity", payload, "activity-rollback")

        def fail_write(mapper, connection, target):
            raise RuntimeError("INJECTED_FAILURE")

        event.listen(Activity, "before_insert", fail_write)
        try:
            try:
                apply(service, alice, fault)
            except RuntimeError as exc:
                assert str(exc) == "INJECTED_FAILURE"
            else:
                raise AssertionError("Failure injection did not run")
        finally:
            event.remove(Activity, "before_insert", fail_write)
        assert service.get_action(alice, fault["action_id"])["status"] == "pending"
        assert service.list_activities(alice, today(), today())["record_count"] == 1
        apply(service, alice, fault)
        checks.append("atomic_rollback_and_retry")

        concurrent = service.propose(alice, "record_activity", payload, "activity-concurrent")
        barrier = Barrier(2)

        def simultaneous_confirm(_):
            barrier.wait(timeout=10)
            return apply(service, alice, concurrent)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(simultaneous_confirm, range(2)))
        assert results[0]["result"] == results[1]["result"]
        assert service.list_activities(alice, today(), today())["record_count"] == 3
        checks.append("concurrent_confirmation_single_write")
        return checks
    finally:
        # 不TRUNCATE、不删表，只删本次随机用户及其关联合成记录。
        with Session(engine) as session, session.begin():
            for table in (Activity, Profile, PendingAction):
                session.execute(delete(table).where(table.owner_id.in_([str(o) for o in owners])))
            session.execute(delete(User).where(User.id.in_([str(o) for o in owners])))
        with Session(engine) as session:
            for table in (Activity, Profile, PendingAction):
                assert (
                    session.scalar(
                        select(table).where(table.owner_id.in_([str(o) for o in owners]))
                    )
                    is None
                )
            assert session.scalar(select(User).where(User.id.in_([str(o) for o in owners]))) is None


def main():
    parser = argparse.ArgumentParser(description="FitLife测试库迁移及业务验收（非模型演示）")
    parser.add_argument("--schema-only", action="store_true", help="只生成迁移SQL，不连接数据库")
    args = parser.parse_args()
    if args.schema_only:
        command.upgrade(migration_config(), "head", sql=True)
        return

    password = database_password("test")
    engine = DatabaseSettings(environment="test", password=password).engine()
    report = {
        "database": "fitlife_test",
        "account": "fit_test",
        "status": "failed",
        "phase": "connect",
        "checks": [],
    }
    try:
        with engine.connect() as connection:
            account, database = connection.execute(text("SELECT CURRENT_USER(), DATABASE()")).one()
            assert account.split("@", 1)[0] == "fit_test" and database == "fitlife_test"
            tables = set(inspect(connection).get_table_names())
            owned_tables = set(Base.metadata.tables)
            if owned_tables & tables and "fl_alembic_version" not in tables:
                raise RuntimeError(
                    "Existing FitLife tables without migration version; inspect first"
                )
            connection.rollback()
            report["phase"] = "migration"
            config = migration_config()
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            connection.commit()
            report["phase"] = "schema_comparison"
            diff = compare_metadata(
                MigrationContext.configure(
                    connection,
                    opts={
                        "include_object": lambda obj, name, kind, reflected, other: (
                            kind != "table" or name in owned_tables
                        ),
                        "version_table": "fl_alembic_version",
                    },
                ),
                Base.metadata,
            )
            assert not diff, "Migration and ORM schema differ"
        report["phase"] = "business_checks"
        verify(engine, report["checks"])
        report["fixture_cleanup"] = "verified"
        report["status"] = "passed"
        report["phase"] = "completed"
    except Exception as exc:
        report["error_type"] = type(exc).__name__
        driver_args = getattr(getattr(exc, "orig", None), "args", ())
        if driver_args and isinstance(driver_args[0], int):
            report["database_error_number"] = driver_args[0]
        if isinstance(exc, BusinessError):
            report["error_code"] = exc.code
        # 不输出异常正文、SQL参数或连接URL。
    finally:
        engine.dispose()
    destination = ROOT / "artifacts" / "business-checks" / f"{uuid4()}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"验收：{report['status']}；报告：{destination}")
    if report["status"] != "passed":
        print(f"错误类型：{report.get('error_type', 'unknown')}；未输出凭据或SQL参数。")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
