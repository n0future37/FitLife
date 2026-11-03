"""Migrate body/diet/exercise textual records to isolated MySQL storage."""

import sqlalchemy as sa
from alembic import op

revision = "0005_domain_records"
down_revision = "0004_conversations"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "fl_domain_records",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("fl_users.id"), nullable=False),
        sa.Column("record_type", sa.String(32), nullable=False),
        sa.Column("record_key", sa.String(100), nullable=False),
        sa.Column("values", sa.JSON, nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column(
            "source_action_id",
            sa.String(36),
            sa.ForeignKey("fl_pending_actions.id"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime, nullable=False),
        sa.UniqueConstraint("owner_id", "record_type", "record_key", name="uq_fl_domain_scope"),
        sa.CheckConstraint("version >= 1", name="ck_fl_domain_version"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_bin",
    )
    op.create_index("ix_fl_domain_records_owner_id", "fl_domain_records", ["owner_id"])


def downgrade():
    op.drop_table("fl_domain_records")
