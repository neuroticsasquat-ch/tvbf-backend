"""add catalog.show.match_method (NEU-1043)

Records which of the migration's three mapping tiers attached a row's `tmdb_id`
— `tvdb_id` and `imdb_id` (both exact `/find` lookups) or `title_year` (the
title + year search, which is a judgement even under its three-way guard).

Without it, an exact match and a guessed one are indistinguishable forever. With
it, tier 3 can be read more sceptically, audited, or retracted as a batch.

NULL means the migration did not map the row: nothing matched it, or the TMDB
ingest inserted it directly and never had to match anything.

Revision ID: b7d3e5c81f04
Revises: aa4571de8f17
Create Date: 2026-08-09 23:40:00.000000+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7d3e5c81f04"
down_revision: str | Sequence[str] | None = "aa4571de8f17"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("show", sa.Column("match_method", sa.Text(), nullable=True), schema="catalog")
    # Our vocabulary, so a CHECK is right — a value outside it is a bug in our
    # own writer, not an upstream string we do not control.
    op.create_check_constraint(
        "ck_show_match_method",
        "show",
        "match_method IS NULL OR match_method IN ('tvdb_id', 'imdb_id', 'title_year')",
        schema="catalog",
    )


def downgrade() -> None:
    op.drop_constraint("ck_show_match_method", "show", schema="catalog", type_="check")
    op.drop_column("show", "match_method", schema="catalog")
