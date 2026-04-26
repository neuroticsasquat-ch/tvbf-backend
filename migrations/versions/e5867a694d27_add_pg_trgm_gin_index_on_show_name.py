"""add pg_trgm gin index on show name

Revision ID: e5867a694d27
Revises: 4fbc8b3da8b8
Create Date: 2026-04-26 16:39:28.271894+00:00

The /shows search uses per-token ILIKE substring matching. With ~80k shows in
the catalog, that's a sequential scan per token unless we have a trigram index.
A GIN index using gin_trgm_ops can serve LIKE/ILIKE queries directly for tokens
of length >= 3.
"""

from alembic import op

revision = "e5867a694d27"
down_revision = "4fbc8b3da8b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_show_name_trgm "
        "ON tvmaze.show USING gin (name gin_trgm_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS tvmaze.ix_show_name_trgm")
    # Leave pg_trgm extension in place — cheap, possibly used elsewhere.
