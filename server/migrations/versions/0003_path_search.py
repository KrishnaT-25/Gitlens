"""Include filenames in snapshot search.

Revision ID: 0003_path_search
Revises: 0002_github_oauth
"""
from alembic import op

revision = "0003_path_search"
down_revision = "0002_github_oauth"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_index("ix_chunks_search", table_name="chunks")
    op.execute("CREATE INDEX ix_chunks_search ON chunks USING GIN (to_tsvector('simple', path || ' ' || text))")


def downgrade():
    op.drop_index("ix_chunks_search", table_name="chunks")
    op.execute("CREATE INDEX ix_chunks_search ON chunks USING GIN (to_tsvector('simple', text))")
