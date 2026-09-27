"""add catalog.show.last_aired, backfilled, with its browse index (NEU-1502)

**Last aired** (CONTEXT.md) stored on the spine so browse's "Last Aired" sort
stops computing a per-row aggregate over every episode. The backfill is an
inline copy of `tvbf.catalog.last_aired.recompute_last_aired`'s statement —
deliberately, since no migration imports application code — and the specials
predicate is `catalog/episodes.py`'s `IS_SPECIAL` written out. Edit the module,
not this; the daily delta's roll-forward re-derives every row anyway.

`today` is the server's UTC date, bound in, as every caller of the module does.

Autogenerate also proposed dropping the four `*_trgm` indexes: they exist only
in migrations (`aa4571de8f17` and before), never on the models, and are exactly
what search reads. Those drops are not here.

Revision ID: 28e392fdeb47
Revises: d2a8f6c41e07
Create Date: 2026-09-27 19:53:04.877067+00:00

"""

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "28e392fdeb47"
down_revision: str | Sequence[str] | None = "d2a8f6c41e07"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BACKFILL = """
UPDATE catalog.show AS s
   SET last_aired = agg.last_aired
  FROM (
        SELECT show_id, max(air_date) AS last_aired
          FROM catalog.episode
         WHERE air_date <= :today
           AND NOT (season_number = 0 OR episode_number < 0)
         GROUP BY show_id
       ) AS agg
 WHERE s.id = agg.show_id
"""


def upgrade() -> None:
    op.add_column("show", sa.Column("last_aired", sa.Date(), nullable=True), schema="catalog")
    # A new column is NULL everywhere, so the inner join is the whole backfill:
    # a show with no qualifying episode is already what the module would set.
    op.execute(sa.text(BACKFILL).bindparams(today=datetime.now(UTC).date()))
    op.create_index(
        "ix_show_last_aired_live",
        "show",
        [sa.literal_column("last_aired DESC NULLS LAST"), "id"],
        unique=False,
        schema="catalog",
        postgresql_where=sa.text("deleted_upstream_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_show_last_aired_live", table_name="show", schema="catalog")
    op.drop_column("show", "last_aired", schema="catalog")
