"""本机服务启动：隐藏输入数据库密码，不在文件/命令参数保存凭据。"""

import argparse

import uvicorn
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from pydantic import SecretStr
from sqlalchemy import text

from fitlife.api import create_app
from fitlife.capacity import RedisCapacity, RedisSettings
from fitlife.credentials import database_password
from fitlife.database import DatabaseSettings
from fitlife.db_check import migration_config
from fitlife.http_protocol import NoDelayH11Protocol
from fitlife.model import Settings


def main():
    parser = argparse.ArgumentParser(description="启动FitLife认证API")
    parser.add_argument("--environment", choices=["dev", "test"], default="dev")
    parser.add_argument("--port", type=int, default=8100)
    parser.add_argument("--host", choices=["127.0.0.1", "0.0.0.0"], default="127.0.0.1")
    parser.add_argument("--local-limit", action="store_true", help="显式只用单进程容量限制")
    parser.add_argument("--redis-credentials-file", help="既有本机Redis安全配置路径，不是密码")
    args = parser.parse_args()
    settings = DatabaseSettings(environment=args.environment)
    if settings.password is None:
        settings.password = SecretStr(
            database_password(args.environment, settings.host, settings.port)
        )
    engine = settings.engine()
    try:
        with engine.connect() as connection:
            account, database = connection.execute(text("SELECT CURRENT_USER(), DATABASE()")).one()
            if (
                account.split("@", 1)[0] != f"fit_{args.environment}"
                or database != f"fitlife_{args.environment}"
            ):
                raise ValueError("DATABASE_IDENTITY_MISMATCH")
            actual = MigrationContext.configure(
                connection, opts={"version_table": "fl_alembic_version"}
            ).get_current_revision()
            expected = ScriptDirectory.from_config(migration_config()).get_current_head()
            if actual != expected:
                raise ValueError("MIGRATION_REQUIRED")
        redis_settings = RedisSettings()
        if args.redis_credentials_file:
            from pathlib import Path

            redis_settings.credentials_file = Path(args.redis_credentials_file)
        capacity = (
            None
            if args.local_limit
            else RedisCapacity(
                redis_settings,
                args.environment,
                ttl=max(Settings().run_timeout, Settings().master_run_timeout) + 30,
            )
        )
        app = create_app(settings, engine=engine, capacity=capacity, media_worker_enabled=True)
        uvicorn.run(app, host=args.host, port=args.port, http=NoDelayH11Protocol, access_log=False)
    except Exception as exc:
        print(f"服务未正常启动/结束：{type(exc).__name__}。请检查本机配置及迁移版本。")
        raise SystemExit(1) from None
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
