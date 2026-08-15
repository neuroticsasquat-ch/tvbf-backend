"""Cache the oracle's show id so the airdate pass stops re-resolving it (NEU-1148)

`catalog.airdate_show_state` holds one row per show the nightly airdate pass has
asked TV Maze about. The pass spent two requests per show — a `/lookup/shows` by
external id, then the episode list — and the first returns the same answer every
night. It is entirely rate-limiter-bound, so halving the requests roughly halves
the wall clock.

A sidecar rather than a column on `catalog.show`, for the reason
`air_date_offset` is one: ADR-0012 sole-sources the spine from TMDB, and a TV
Maze id is not TMDB's. It also gives the negative result a home — `tvmaze_id
IS NULL` with a `resolved_at` means "we looked and TV Maze does not carry this
show", which a column on the spine could not tell apart from "nobody asked".

No unique constraint or index on `tvmaze_id`: two of our shows legitimately
resolving to one TV Maze show is the shape NEU-1146 spent four match tiers on,
so a unique constraint would fire on correct data. Every access is by `show_id`,
which is the primary key directly.

The table is a pure derived cache — the downgrade drops it and the next run
rebuilds whatever it needs.

Revision ID: c9a41b2e77d5
Revises: b3d7c1f04ae9
Create Date: 2026-08-14 18:00:00.000000+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c9a41b2e77d5"
down_revision: str | Sequence[str] | None = "b3d7c1f04ae9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "airdate_show_state",
        sa.Column("show_id", sa.BigInteger(), nullable=False),
        # NULL is the negative result: we asked and TV Maze does not carry it.
        sa.Column("tvmaze_id", sa.Integer(), nullable=True),
        # When we last asked, whichever answer came back. NOT NULL, so "never
        # asked" is the absence of a row and nothing else.
        sa.Column(
            "resolved_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["show_id"],
            ["catalog.show.id"],
            name=op.f("fk_airdate_show_state_show_id_show"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("show_id", name=op.f("pk_airdate_show_state")),
        schema="catalog",
    )


def downgrade() -> None:
    op.drop_table("airdate_show_state", schema="catalog")
