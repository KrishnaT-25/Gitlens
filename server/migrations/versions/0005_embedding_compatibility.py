"""Identify embedding spaces; legacy vectors remain unreadable until rebuilt.

Revision ID: 0005_embedding_compatibility
Revises: 0004_agentic_rag
"""
from alembic import op
import sqlalchemy as sa

revision = "0005_embedding_compatibility"
down_revision = "0004_agentic_rag"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("chunk_embeddings", sa.Column("fingerprint", sa.String(64), nullable=True))
    op.add_column("chunk_embeddings", sa.Column("dimensions", sa.Integer(), nullable=False, server_default="768"))
    op.add_column("chunk_embeddings", sa.Column("format_version", sa.Integer(), nullable=False, server_default="0"))
    op.create_index("ix_chunk_embeddings_fingerprint", "chunk_embeddings", ["fingerprint"])
    # Unknown legacy formatting must never be labelled compatible. Preserve the
    # text index and summaries while the normal leased worker rebuilds vectors.
    op.execute("""
        UPDATE snapshots SET index_report = (
            (COALESCE(index_report::jsonb, '{}'::jsonb) - 'embedding' - 'embeddingRepairAttempt')
            || '{"semantic":"keyword_only","semanticErrorCode":"embedding_incompatible"}'::jsonb
        )::json WHERE status='ready'
    """)
    op.execute("""
        UPDATE jobs SET status='queued', attempts=0, lease_until=NULL, lease_token=NULL,
               dispatched_at=NULL, next_attempt_at=now(), error_code=NULL
        FROM snapshots s WHERE jobs.snapshot_id=s.id AND s.status='ready'
          AND jobs.status IN ('done', 'failed')
    """)
    op.execute("""
        INSERT INTO jobs (id, snapshot_id, status, attempts, next_attempt_at)
        SELECT gen_random_uuid()::text, s.id, 'queued', 0, now()
        FROM snapshots s LEFT JOIN jobs j ON j.snapshot_id=s.id
        WHERE s.status='ready' AND j.id IS NULL
    """)


def downgrade():
    op.drop_index("ix_chunk_embeddings_fingerprint", table_name="chunk_embeddings")
    op.drop_column("chunk_embeddings", "format_version")
    op.drop_column("chunk_embeddings", "dimensions")
    op.drop_column("chunk_embeddings", "fingerprint")
