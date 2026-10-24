"""Add GitHub identities without changing existing email users.

Revision ID: 0002_github_oauth
Revises: 0001_initial
"""
import sqlalchemy as sa
from alembic import op

revision = "0002_github_oauth"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("github_id", sa.String(40), nullable=True))
    op.add_column("users", sa.Column("github_login", sa.String(100), nullable=True))
    op.alter_column("users", "email", existing_type=sa.String(320), nullable=True)
    op.create_index("ix_users_github_id", "users", ["github_id"], unique=True)
    op.create_check_constraint("ck_user_identity", "users", "email IS NOT NULL OR github_id IS NOT NULL")


def downgrade():
    op.execute("DELETE FROM sessions WHERE user_id IN (SELECT id FROM users WHERE email IS NULL)")
    op.execute("DELETE FROM users WHERE email IS NULL")
    op.drop_constraint("ck_user_identity", "users", type_="check")
    op.drop_index("ix_users_github_id", table_name="users")
    op.alter_column("users", "email", existing_type=sa.String(320), nullable=False)
    op.drop_column("users", "github_login")
    op.drop_column("users", "github_id")
