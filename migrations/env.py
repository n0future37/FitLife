"""只从环境/调用方连接获取配置，配置文件不含凭据。"""

from alembic import context

from fitlife.database import Base, DatabaseSettings


def migrate(connection=None):
    options = dict(target_metadata=Base.metadata, version_table="fl_alembic_version")
    if connection is None:
        context.configure(dialect_name="mysql", literal_binds=True, **options)
    else:
        context.configure(connection=connection, **options)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    migrate()
elif context.config.attributes.get("connection") is not None:
    migrate(context.config.attributes["connection"])
else:
    engine = DatabaseSettings().engine()
    try:
        with engine.connect() as connection:
            migrate(connection)
    finally:
        engine.dispose()
