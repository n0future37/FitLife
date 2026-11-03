"""画像、活动与待确认操作；固定迁移，不依赖未来ORM模型定义。"""

import sqlalchemy as sa
from alembic import op

revision = "0001_profile_activity"
down_revision = None
branch_labels = None
depends_on = None
MYSQL = dict(mysql_engine="InnoDB", mysql_charset="utf8mb4", mysql_collate="utf8mb4_bin")


def upgrade():
    op.create_table("fl_users", sa.Column("id", sa.String(36), primary_key=True), **MYSQL)
    op.create_table(
        "fl_pending_actions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("fl_users.id"), nullable=False),
        sa.Column("request_key", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("base_profile_version", sa.Integer, nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("expires_at", sa.DateTime, nullable=False),
        sa.Column("result", sa.JSON, nullable=True),
        sa.UniqueConstraint("owner_id", "request_key", "kind", name="uq_fl_request"),
        **MYSQL,
    )
    op.create_index("ix_fl_pending_actions_owner_id", "fl_pending_actions", ["owner_id"])
    op.create_table(
        "fl_profiles",
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("fl_users.id"), primary_key=True),
        sa.Column("values", sa.JSON, nullable=False),
        sa.Column("provenance", sa.JSON, nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
        sa.CheckConstraint("version >= 1", name="ck_fl_profile_version"),
        **MYSQL,
    )
    op.create_table(
        "fl_activities",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("fl_users.id"), nullable=False),
        sa.Column(
            "source_action_id",
            sa.String(36),
            sa.ForeignKey("fl_pending_actions.id"),
            nullable=False,
        ),
        sa.Column("activity_id", sa.String(40), nullable=False),
        sa.Column("occurred_on", sa.Date, nullable=False),
        sa.Column("minutes", sa.Integer, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.UniqueConstraint("source_action_id", name="uq_fl_activity_action"),
        sa.CheckConstraint("minutes >= 1 AND minutes <= 1440", name="ck_fl_activity_minutes"),
        **MYSQL,
    )
    op.create_index("ix_fl_activities_owner_id", "fl_activities", ["owner_id"])
    op.create_index("ix_fl_activities_occurred_on", "fl_activities", ["occurred_on"])


def downgrade():
    # 仅在显式执行降级命令时删除本迁移的表；验收脚本不调用降级。
    for table in ("fl_activities", "fl_profiles", "fl_pending_actions", "fl_users"):
        op.drop_table(table)
