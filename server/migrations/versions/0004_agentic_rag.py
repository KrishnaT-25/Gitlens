"""Add versioned repository maps, AST metadata, references, and embeddings.

Revision ID: 0004_agentic_rag
Revises: 0003_path_search
"""
from alembic import op
import sqlalchemy as sa

class Vector768(sa.types.UserDefinedType):
    def get_col_spec(self, **_kw):
        return "vector(768)"

revision = "0004_agentic_rag"
down_revision = "0003_path_search"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("DO $$ BEGIN CREATE EXTENSION IF NOT EXISTS pg_search; EXCEPTION WHEN undefined_file OR insufficient_privilege THEN RAISE NOTICE 'pg_search unavailable or not permitted; BM25 will use PostgreSQL FTS fallback'; END $$")
    op.add_column("snapshots", sa.Column("index_version", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("snapshots", sa.Column("index_report", sa.JSON(), nullable=True))
    op.add_column("chunks", sa.Column("index_version", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("chunks", sa.Column("language", sa.String(40), nullable=True))
    op.add_column("chunks", sa.Column("symbol_path", sa.String(1000), nullable=True))
    op.add_column("chunks", sa.Column("syntax_kind", sa.String(80), nullable=True))
    op.create_index("ix_chunks_index_version", "chunks", ["index_version"])
    op.create_index("ix_chunk_snapshot_path_line", "chunks", ["snapshot_id", "index_version", "path", "line_start"])
    op.create_unique_constraint("uq_chunk_snapshot_version", "chunks", ["id", "snapshot_id", "index_version"])
    op.create_table(
        "snapshot_files",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("snapshot_id", sa.String(36), sa.ForeignKey("snapshots.id", ondelete="CASCADE"), nullable=False),
        sa.Column("index_version", sa.Integer(), nullable=False),
        sa.Column("path", sa.String(1000), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("language", sa.String(40)),
        sa.Column("line_count", sa.Integer(), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.UniqueConstraint("snapshot_id", "index_version", "path"),
    )
    op.create_index("ix_snapshot_files_path", "snapshot_files", ["snapshot_id", "index_version", "path"])
    op.create_table(
        "snapshot_nodes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("snapshot_id", sa.String(36), sa.ForeignKey("snapshots.id", ondelete="CASCADE"), nullable=False),
        sa.Column("index_version", sa.Integer(), nullable=False),
        sa.Column("path", sa.String(1000), nullable=False),
        sa.Column("parent_path", sa.String(1000), nullable=False, server_default=""),
        sa.Column("node_type", sa.String(16), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.UniqueConstraint("snapshot_id", "index_version", "path", "node_type"),
    )
    op.create_index("ix_snapshot_nodes_parent", "snapshot_nodes", ["snapshot_id", "index_version", "parent_path"])
    op.create_table(
        "symbol_references",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("snapshot_id", sa.String(36), sa.ForeignKey("snapshots.id", ondelete="CASCADE"), nullable=False),
        sa.Column("index_version", sa.Integer(), nullable=False),
        sa.Column("chunk_id", sa.String(36), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("path", sa.String(1000), nullable=False),
        sa.Column("line", sa.Integer(), nullable=False),
        sa.Column("is_definition", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.ForeignKeyConstraint(["chunk_id", "snapshot_id", "index_version"],
                                ["chunks.id", "chunks.snapshot_id", "chunks.index_version"],
                                ondelete="CASCADE", name="fk_symbol_reference_chunk_snapshot"),
    )
    op.create_index("ix_symbol_references_lookup", "symbol_references", ["snapshot_id", "index_version", "name"])
    op.create_table(
        "chunk_embeddings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("chunk_id", sa.String(36), sa.ForeignKey("chunks.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("embedding", Vector768(), nullable=False),
    )
    op.create_index("ix_chunk_embeddings_hnsw", "chunk_embeddings", ["embedding"], postgresql_using="hnsw", postgresql_ops={"embedding": "vector_cosine_ops"})
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_search') THEN
            EXECUTE 'CREATE INDEX ix_chunks_bm25 ON chunks USING bm25 (id, path, symbol_path, text) WITH (key_field = ''id'')';
          END IF;
        END $$
    """)
    # Keep the current version-1 index readable while scheduling an additive version-2 rebuild.
    op.execute("""
        UPDATE jobs SET status='queued', attempts=0, lease_until=NULL, lease_token=NULL,
               next_attempt_at=now(), error_code=NULL
        FROM snapshots s
        WHERE jobs.snapshot_id=s.id AND s.status='ready' AND s.index_version < 2
          AND jobs.status IN ('done', 'failed')
    """)
    op.execute("""
        INSERT INTO jobs (id, snapshot_id, status, attempts, next_attempt_at)
        SELECT gen_random_uuid()::text, s.id, 'queued', 0, now()
        FROM snapshots s LEFT JOIN jobs j ON j.snapshot_id=s.id
        WHERE s.status='ready' AND s.index_version < 2 AND j.id IS NULL
    """)


def downgrade():
    op.execute("DROP INDEX IF EXISTS ix_chunks_bm25")
    op.drop_index("ix_chunk_embeddings_hnsw", table_name="chunk_embeddings")
    op.drop_table("chunk_embeddings")
    op.drop_index("ix_symbol_references_lookup", table_name="symbol_references")
    op.drop_table("symbol_references")
    op.drop_index("ix_snapshot_nodes_parent", table_name="snapshot_nodes")
    op.drop_table("snapshot_nodes")
    op.drop_index("ix_snapshot_files_path", table_name="snapshot_files")
    op.drop_table("snapshot_files")
    op.drop_index("ix_chunks_index_version", table_name="chunks")
    op.drop_index("ix_chunk_snapshot_path_line", table_name="chunks")
    op.drop_constraint("uq_chunk_snapshot_version", "chunks", type_="unique")
    op.drop_column("chunks", "syntax_kind")
    op.drop_column("chunks", "symbol_path")
    op.drop_column("chunks", "language")
    op.drop_column("chunks", "index_version")
    op.drop_column("snapshots", "index_report")
    op.drop_column("snapshots", "index_version")
