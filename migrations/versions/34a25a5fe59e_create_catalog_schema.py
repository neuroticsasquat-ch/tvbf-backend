"""create catalog schema

The source-neutral catalog schema that replaces `tvmaze` (ADR-0007). No tables
yet — this only makes the namespace exist, so the first migration that adds a
`catalog` table has somewhere to put it. `task db:init` covers localdev and
`.github/workflows/test.yml` covers CI, but nothing runs either in prod, where
migrations are the only DDL that lands.

Revision ID: 34a25a5fe59e
Revises: 9c31f0a5b7d2
Create Date: 2026-08-09

"""

from collections.abc import Sequence

from alembic import op

revision: str = "34a25a5fe59e"
down_revision: str | Sequence[str] | None = "9c31f0a5b7d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Idempotent: a database that already got the schema from db:init or CI is
    # unaffected.
    op.execute("CREATE SCHEMA IF NOT EXISTS catalog")


def downgrade() -> None:
    # RESTRICT, not CASCADE — this only unwinds while the schema is still
    # empty. Once `app` references catalog rows, a CASCADE here would take
    # user data with it, and no upstream re-fetch could put it back.
    op.execute("DROP SCHEMA IF EXISTS catalog RESTRICT")
