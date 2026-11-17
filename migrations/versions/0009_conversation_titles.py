"""Conversation names with user priority and optimistic title versions."""

import sqlalchemy as sa
from alembic import op

revision = "0009_conversation_titles"
down_revision = "0008_memory_scope"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "fl_conversations",
        sa.Column("title", sa.String(80), nullable=False, server_default="新对话"),
    )
    op.add_column(
        "fl_conversations",
        sa.Column("title_source", sa.String(12), nullable=False, server_default="pending"),
    )
    op.add_column(
        "fl_conversations",
        sa.Column("title_revision", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade():
    for name in ("title_revision", "title_source", "title"):
        op.drop_column("fl_conversations", name)
