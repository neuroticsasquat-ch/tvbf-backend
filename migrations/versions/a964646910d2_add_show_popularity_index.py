"""add the browse index for the popularity sort (NEU-1513)

`ORDER BY popularity DESC NULLS LAST, id` over live shows becomes an index
walk, on `ix_show_last_aired_live`'s shape (NEU-1502). No column and no
backfill: `catalog.show.popularity` has been mirrored since the TMDB cutover
and is refreshed nightly from the id export (NEU-1172).

Autogenerate also proposed dropping the five `*_trgm` indexes: they exist only
in migrations (`aa4571de8f17` and before), never on the models, and are exactly
what search reads. Those drops are not here.

Revision ID: a964646910d2
Revises: 212671cf449f
Create Date: 2026-10-01 18:49:54.281458+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a964646910d2"
down_revision: str | Sequence[str] | None = "212671cf449f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_show_popularity_live",
        "show",
        [sa.literal_column("popularity DESC NULLS LAST"), "id"],
        unique=False,
        schema="catalog",
        postgresql_where=sa.text("deleted_upstream_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_show_popularity_live", table_name="show", schema="catalog")
