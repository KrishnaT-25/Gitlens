"""Store chat request claims and results for safe polling and retry."""

from alembic import op
import sqlalchemy as sa

revision = "0006_chat_requests"
down_revision = "0005_embedding_compatibility"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "chat_requests",
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("request_id", sa.String(36), primary_key=True),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("result", sa.JSON()),
    )
    op.create_index("ix_chat_requests_deadline", "chat_requests", ["deadline_at"])


def downgrade():
    op.drop_table("chat_requests")
