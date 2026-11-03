"""持久化会话和请求上下文快照；不迁入旧聊天。"""

import sqlalchemy as sa
from alembic import op

revision = "0004_conversations"
down_revision = "0003_weekly_plans"
branch_labels = None
depends_on = None
MYSQL = dict(mysql_engine="InnoDB", mysql_charset="utf8mb4", mysql_collate="utf8mb4_bin")


def upgrade():
    op.create_table(
        "fl_conversations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("fl_users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
        sa.Column("active_request_key", sa.String(36), nullable=True),
        sa.Column("lease_until", sa.DateTime, nullable=False),
        sa.Column("deleted", sa.Boolean, nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        **MYSQL,
    )
    op.create_index("ix_fl_conversations_owner_id", "fl_conversations", ["owner_id"])
    op.add_column("fl_agent_requests", sa.Column("conversation_id", sa.String(36), nullable=True))
    op.create_foreign_key(
        "fk_fl_request_conversation",
        "fl_agent_requests",
        "fl_conversations",
        ["conversation_id"],
        ["id"],
    )
    op.create_index(
        "ix_fl_agent_requests_conversation_id", "fl_agent_requests", ["conversation_id"]
    )
    op.add_column("fl_agent_requests", sa.Column("user_message", sa.String(4000), nullable=True))
    op.add_column("fl_agent_requests", sa.Column("context", sa.JSON, nullable=True))
    op.add_column("fl_agent_requests", sa.Column("turn_index", sa.Integer, nullable=True))
    op.add_column("fl_agent_requests", sa.Column("memory_version", sa.Integer, nullable=True))


def downgrade():
    op.drop_column("fl_agent_requests", "memory_version")
    op.drop_column("fl_agent_requests", "turn_index")
    op.drop_column("fl_agent_requests", "context")
    op.drop_column("fl_agent_requests", "user_message")
    op.drop_constraint("fk_fl_request_conversation", "fl_agent_requests", type_="foreignkey")
    op.drop_index("ix_fl_agent_requests_conversation_id", "fl_agent_requests")
    op.drop_column("fl_agent_requests", "conversation_id")
    op.drop_table("fl_conversations")
