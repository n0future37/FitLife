"""可信本机管理入口：迁移、创建内部用户和签发个人访问令牌。"""

import argparse
from uuid import UUID

from alembic import command
from pydantic import SecretStr
from sqlalchemy import inspect, text

from fitlife.auth import issue_token
from fitlife.credentials import database_password
from fitlife.database import Base, DatabaseSettings
from fitlife.db_check import migration_config


def migrate(engine, environment):
    with engine.connect() as connection:
        account, database = connection.execute(text("SELECT CURRENT_USER(), DATABASE()")).one()
        if account.split("@", 1)[0] != f"fit_{environment}" or database != f"fitlife_{environment}":
            raise ValueError("DATABASE_IDENTITY_MISMATCH")
        tables = set(inspect(connection).get_table_names())
        if set(Base.metadata.tables) & tables and "fl_alembic_version" not in tables:
            raise ValueError("EXISTING_SCHEMA_REQUIRES_INSPECTION")
        connection.rollback()
        config = migration_config()
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
        connection.commit()


def main():
    parser = argparse.ArgumentParser(description="本机管理：迁移并签发一个个人访问令牌")
    parser.add_argument("--environment", required=True, choices=["dev", "test"])
    parser.add_argument("--user-id", type=UUID, help="给已有用户签发新令牌；不填则创建用户")
    args = parser.parse_args()
    settings = DatabaseSettings(environment=args.environment)
    if settings.password is None:
        settings.password = SecretStr(
            database_password(args.environment, settings.host, settings.port)
        )
    engine = settings.engine()
    try:
        migrate(engine, args.environment)
        actor, token = issue_token(engine, args.user_id)
        print(f"用户ID：{actor.user_id}")
        print("个人访问令牌（仅本机显示这一次；默认24小时有效，请勿发到聊天或写入日志）：")
        print(token)
    except Exception as exc:
        print(f"管理操作失败：{type(exc).__name__}，未输出异常正文或数据库凭据。")
        raise SystemExit(1) from None
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
