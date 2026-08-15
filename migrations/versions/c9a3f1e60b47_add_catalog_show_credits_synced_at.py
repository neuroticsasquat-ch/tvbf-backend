"""add catalog.show.credits_synced_at (NEU-1127)

The credits backfill's resumability watermark, and the record of a mistake this
schema had no way to represent.

The full catalog ingest finished on 2026-08-10 and the two credit writers merged
on 2026-08-11, so all 228,841 shows carrying `tmdb_synced_at` in production hold
a complete TMDB payload and **no credits at all** — `catalog.show_cast`,
`show_crew`, `episode_guest_cast`, `episode_crew` and `person` are empty tables.
`tmdb_synced_at` cannot say that: it is one bit covering a payload that used to
mean fewer things than it does now.

So the backlog is `tmdb_synced_at IS NOT NULL AND credits_synced_at IS NULL`,
and it empties as `python -m tvbf.jobs.credits_backfill backfill` works through
it. The alternative work list — "the show carries no `show_cast` row" — needs no
migration and is self-clearing the way `enrichment.py`'s is, but it conflates
*upstream has no credits for this show* with *nobody has asked yet*, so every
credit-less series would be re-fetched on every run and the pass would never
converge. That is the same conflation NEU-1034's watermark exists to avoid one
grain up, where "already ingested" and "row already present" were also
different questions.

Nullable with no backfill and no index. Null is the correct value for every
existing row — none of them has credits — and the work list pages by primary key
the way `enrichment.py` and `episode_map.py` do, so the scan starts at the
cursor rather than at the top of a 228k-row table.

Revision ID: c9a3f1e60b47
Revises: b6d24f0ac715
Create Date: 2026-08-12 12:00:00.000000+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c9a3f1e60b47"
down_revision: str | Sequence[str] | None = "b6d24f0ac715"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "show",
        sa.Column("credits_synced_at", sa.DateTime(timezone=True), nullable=True),
        schema="catalog",
    )


def downgrade() -> None:
    op.drop_column("show", "credits_synced_at", schema="catalog")
