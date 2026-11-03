"""Private assets and recoverable, fenced multimodal jobs."""

import sqlalchemy as sa
from alembic import op

revision = "0006_media_jobs"
down_revision = "0005_domain_records"
branch_labels = None
depends_on = None
MYSQL = dict(mysql_engine="InnoDB", mysql_charset="utf8mb4", mysql_collate="utf8mb4_bin")


def upgrade():
    op.add_column("fl_domain_records", sa.Column("source_media", sa.JSON, nullable=True))
    op.create_table(
        "fl_media_assets",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("fl_users.id"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("metadata", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
        **MYSQL,
    )
    op.create_index("ix_fl_media_assets_owner_id", "fl_media_assets", ["owner_id"])
    op.create_table(
        "fl_media_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("fl_users.id"), nullable=False),
        sa.Column("asset_id", sa.String(36), sa.ForeignKey("fl_media_assets.id"), nullable=False),
        sa.Column("request_key", sa.String(36), nullable=False),
        sa.Column("input_digest", sa.String(64), nullable=False),
        sa.Column("task", sa.String(24), nullable=False),
        sa.Column("message", sa.String(2000), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False),
        sa.Column("model_calls", sa.Integer, nullable=False),
        sa.Column("progress", sa.Integer, nullable=False),
        sa.Column("lease_until", sa.DateTime, nullable=False),
        sa.Column("nonce", sa.String(36), nullable=False),
        sa.Column("reviewed", sa.Boolean, nullable=False),
        sa.Column("evidence", sa.JSON, nullable=False),
        sa.Column("partial", sa.JSON, nullable=False),
        sa.Column("result", sa.JSON, nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
        sa.UniqueConstraint("owner_id", "request_key", name="uq_fl_media_request"),
        **MYSQL,
    )
    op.create_index("ix_fl_media_jobs_owner_id", "fl_media_jobs", ["owner_id"])


def downgrade():
    op.drop_table("fl_media_jobs")
    op.drop_table("fl_media_assets")
    op.drop_column("fl_domain_records", "source_media")
