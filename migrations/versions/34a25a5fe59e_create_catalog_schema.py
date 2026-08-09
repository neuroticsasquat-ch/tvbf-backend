"""create catalog schema

Revision ID: 34a25a5fe59e
Revises: 9c31f0a5b7d2
Create Date: 2026-08-09 16:33:40.441064+00:00

"""

from alembic import op

revision = "34a25a5fe59e"
down_revision = "9c31f0a5b7d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # `task db:init` creates the schema locally, but nothing runs that in
    # prod — migrations are the only DDL that reaches it. Creating the schema
    # here means the first migration that adds a `catalog` table has somewhere
    # to put it. Idempotent so a database that already got it from db:init is
    # unaffected.
    op.execute("CREATE SCHEMA IF NOT EXISTS catalog")


def downgrade() -> None:
    # Only drops while still empty — a populated catalog is not something a
    # downgrade should destroy, and CASCADE here would take user data with it
    # once `app` references catalog rows.
    op.execute("DROP SCHEMA IF EXISTS catalog RESTRICT")
