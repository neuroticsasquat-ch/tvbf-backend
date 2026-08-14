"""Add the airdate reconciliation watermark (NEU-1149)

`catalog.airdate_show_state.last_reconciled_at` is when a show's turn in the
nightly airdate pass last completed without raising. It is the other half of a
comparison whose first half already exists: `catalog.show.tmdb_synced_at`
advances precisely when TMDB reported a change and we re-mirrored the show, so
`tmdb_synced_at > last_reconciled_at` answers "did TMDB touch this show since we
last looked?" with no new signal.

Without it the pass re-derives every offset every night, which makes its cost
proportional to catalog size plus user count when the thing that needs
re-checking is proportional to change. The population that grows with the user
base — tracked shows that have finished airing — is exactly the one where
nightly re-checking buys nothing.

Nullable, and deliberately not backfilled: NULL is the "never reconciled" state
the work list's first clause reads, so the first run after deploy covers the
full scope once at today's cost and the buckets spread the sweep from the night
after. The column lands in the table NEU-1148 created rather than one of its
own, per that table's own docstring — one table for what the airdate pass knows
about a show.

Revision ID: d5f2a91c6b03
Revises: c9a41b2e77d5
Create Date: 2026-08-14 20:00:00.000000+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d5f2a91c6b03"
down_revision: str | Sequence[str] | None = "c9a41b2e77d5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "airdate_show_state",
        sa.Column("last_reconciled_at", sa.DateTime(timezone=True), nullable=True),
        schema="catalog",
    )


def downgrade() -> None:
    op.drop_column("airdate_show_state", "last_reconciled_at", schema="catalog")
