"""周计划及不可变版本；扩展确认基线，不修改旧操作摘要。"""

import sqlalchemy as sa
from alembic import op

revision = "0003_weekly_plans"
down_revision = "0002_auth_requests"
branch_labels = None
depends_on = None
MYSQL = dict(mysql_engine="InnoDB", mysql_charset="utf8mb4", mysql_collate="utf8mb4_bin")


def upgrade():
    op.add_column("fl_pending_actions", sa.Column("base_plan_version", sa.Integer, nullable=True))
    op.add_column(
        "fl_pending_actions", sa.Column("request_fingerprint", sa.String(64), nullable=True)
    )
    op.create_table(
        "fl_plans",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("fl_users.id"), nullable=False),
        sa.Column("week_start", sa.Date, nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
        sa.UniqueConstraint("owner_id", "week_start", name="uq_fl_plan_week"),
        **MYSQL,
    )
    op.create_index("ix_fl_plans_owner_id", "fl_plans", ["owner_id"])
    op.create_table(
        "fl_plan_revisions",
        sa.Column("plan_id", sa.String(36), sa.ForeignKey("fl_plans.id"), primary_key=True),
        sa.Column("version", sa.Integer, primary_key=True),
        sa.Column(
            "source_action_id",
            sa.String(36),
            sa.ForeignKey("fl_pending_actions.id"),
            nullable=False,
        ),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.UniqueConstraint("source_action_id", name="uq_fl_plan_action"),
        **MYSQL,
    )


def downgrade():
    op.drop_table("fl_plan_revisions")
    op.drop_table("fl_plans")
    op.drop_column("fl_pending_actions", "request_fingerprint")
    op.drop_column("fl_pending_actions", "base_plan_version")
