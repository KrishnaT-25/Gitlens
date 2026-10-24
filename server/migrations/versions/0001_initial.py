"""Initial schema.

Revision ID: 0001_initial
Revises:
"""
import sqlalchemy as sa
from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

def upgrade():
    op.create_table("users", sa.Column("id", sa.String(36), primary_key=True), sa.Column("email", sa.String(320), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_users_email", "users", ["email"], unique=True)
    op.create_table("code_challenges", sa.Column("id", sa.String(36), primary_key=True), sa.Column("email", sa.String(320), nullable=False), sa.Column("code_hash", sa.String(64), nullable=False), sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False), sa.Column("attempts", sa.Integer(), nullable=False), sa.Column("consumed_at", sa.DateTime(timezone=True)), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_code_challenges_email", "code_challenges", ["email"])
    op.create_table("sessions", sa.Column("id", sa.String(36), primary_key=True), sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False), sa.Column("token_hash", sa.String(64), nullable=False), sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False), sa.Column("revoked_at", sa.DateTime(timezone=True)))
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])
    op.create_index("ix_sessions_token_hash", "sessions", ["token_hash"], unique=True)
    op.create_table("quotas", sa.Column("id", sa.String(36), primary_key=True), sa.Column("scope", sa.String(128), nullable=False), sa.Column("action", sa.String(32), nullable=False), sa.Column("period", sa.String(32), nullable=False), sa.Column("count", sa.Integer(), nullable=False), sa.UniqueConstraint("scope", "action", "period"), sa.CheckConstraint("count >= 0", name="ck_quota_nonnegative"))
    op.create_table("repositories", sa.Column("id", sa.String(36), primary_key=True), sa.Column("owner", sa.String(100), nullable=False), sa.Column("name", sa.String(100), nullable=False), sa.Column("canonical_url", sa.String(500), nullable=False), sa.Column("default_branch", sa.String(255), nullable=False), sa.UniqueConstraint("owner", "name"))
    op.create_table("snapshots", sa.Column("id", sa.String(36), primary_key=True), sa.Column("repository_id", sa.String(36), sa.ForeignKey("repositories.id"), nullable=False), sa.Column("sha", sa.String(40), nullable=False), sa.Column("status", sa.String(20), nullable=False), sa.Column("error_code", sa.String(64)), sa.Column("summary", sa.JSON()), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False), sa.UniqueConstraint("repository_id", "sha"), sa.CheckConstraint("status IN ('queued', 'indexing', 'summarizing', 'ready', 'failed', 'unsupported')", name="ck_snapshot_status"), sa.CheckConstraint("status <> 'ready' OR summary IS NOT NULL", name="ck_ready_has_summary"))
    op.create_index("ix_snapshots_repository_id", "snapshots", ["repository_id"])
    op.create_index("ix_snapshot_status", "snapshots", ["status"])
    op.create_table("jobs", sa.Column("id", sa.String(36), primary_key=True), sa.Column("snapshot_id", sa.String(36), sa.ForeignKey("snapshots.id"), nullable=False), sa.Column("status", sa.String(20), nullable=False), sa.Column("attempts", sa.Integer(), nullable=False), sa.Column("lease_until", sa.DateTime(timezone=True)), sa.Column("lease_token", sa.String(36)), sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False), sa.Column("dispatched_at", sa.DateTime(timezone=True)), sa.Column("error_code", sa.String(64)), sa.UniqueConstraint("snapshot_id"), sa.CheckConstraint("status IN ('queued', 'running', 'done', 'failed')", name="ck_job_status"), sa.CheckConstraint("attempts >= 0 AND attempts <= 3", name="ck_job_attempts"))
    op.create_index("ix_job_claim", "jobs", ["status", "next_attempt_at"])
    op.create_table("chunks", sa.Column("id", sa.String(36), primary_key=True), sa.Column("snapshot_id", sa.String(36), sa.ForeignKey("snapshots.id"), nullable=False), sa.Column("path", sa.String(1000), nullable=False), sa.Column("line_start", sa.Integer(), nullable=False), sa.Column("line_end", sa.Integer(), nullable=False), sa.Column("text", sa.Text(), nullable=False), sa.CheckConstraint("line_start > 0 AND line_end >= line_start", name="ck_chunk_lines"))
    op.create_index("ix_chunks_snapshot_id", "chunks", ["snapshot_id"])
    op.execute("CREATE INDEX ix_chunks_search ON chunks USING GIN (to_tsvector('simple', text))")

def downgrade():
    op.drop_index("ix_chunks_search", table_name="chunks")
    op.drop_index("ix_chunks_snapshot_id", table_name="chunks")
    op.drop_table("chunks")
    op.drop_index("ix_job_claim", table_name="jobs")
    op.drop_table("jobs")
    op.drop_index("ix_snapshot_status", table_name="snapshots")
    op.drop_index("ix_snapshots_repository_id", table_name="snapshots")
    op.drop_table("snapshots")
    op.drop_table("repositories")
    op.drop_table("quotas")
    op.drop_index("ix_sessions_token_hash", table_name="sessions")
    op.drop_index("ix_sessions_user_id", table_name="sessions")
    op.drop_table("sessions")
    op.drop_index("ix_code_challenges_email", table_name="code_challenges")
    op.drop_table("code_challenges")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")
