"""Versioned public project knowledge, kept separate from private memories."""

import sqlalchemy as sa
from alembic import op

revision = "0007_knowledge"
down_revision = "0006_media_jobs"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "fl_knowledge_chunks",
        sa.Column("id", sa.String(100), primary_key=True),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_bin",
    )


def downgrade():
    op.drop_table("fl_knowledge_chunks")
