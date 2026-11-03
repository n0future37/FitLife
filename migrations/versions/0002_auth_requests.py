"""个人访问令牌及Agent请求去重；不改变既有业务表。"""

import sqlalchemy as sa
from alembic import op

revision = "0002_auth_requests"
down_revision = "0001_profile_activity"
branch_labels = None
depends_on = None
MYSQL = dict(mysql_engine="InnoDB", mysql_charset="utf8mb4", mysql_collate="utf8mb4_bin")


def upgrade():
    op.create_table(
        "fl_access_tokens",
        sa.Column("digest", sa.String(64), primary_key=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("fl_users.id"), nullable=False),
        sa.Column("expires_at", sa.DateTime, nullable=False),
        sa.Column("revoked", sa.Boolean, nullable=False),
        **MYSQL,
    )
    op.create_index("ix_fl_access_tokens_owner_id", "fl_access_tokens", ["owner_id"])
    op.create_table(
        "fl_agent_requests",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("fl_users.id"), nullable=False),
        sa.Column("request_key", sa.String(36), nullable=False),
        sa.Column("message_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("nonce", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("lease_until", sa.DateTime, nullable=False),
        sa.Column("response", sa.JSON, nullable=True),
        sa.UniqueConstraint("owner_id", "request_key", name="uq_fl_agent_request"),
        **MYSQL,
    )
    op.create_index("ix_fl_agent_requests_owner_id", "fl_agent_requests", ["owner_id"])


def downgrade():
    op.drop_table("fl_agent_requests")
    op.drop_table("fl_access_tokens")
