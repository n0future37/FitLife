"""Expire conversation context when effective temporary constraints change."""

import sqlalchemy as sa
from alembic import op

revision = "0008_memory_scope"
down_revision = "0007_knowledge"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("fl_agent_requests", sa.Column("memory_scope", sa.String(64), nullable=True))


def downgrade():
    op.drop_column("fl_agent_requests", "memory_scope")
