"""add catalog.show.tmdb_synced_at and the catalog_initial run kind (NEU-1034)

Two changes, both belonging to the full TMDB catalog ingest.

`catalog.show.tmdb_synced_at` is that ingest's resumability watermark: set once a
complete TMDB payload has been mirrored onto the row, NULL until then. It exists
because "already ingested" and "row already present" are different questions
here. NEU-1042 copied ~89k TV Maze shows into `catalog` and NEU-1043 mapped a
`tmdb_id` onto ~63k of them, so those rows exist and are correctly identified
while still holding TV Maze data — an ingest resuming on row-existence would skip
precisely the shows users track.

`ck_ingest_run_kind` gains `catalog_initial`. The run rows live in
`tvmaze.ingest_run` even though the ingest writes `catalog`: they are operational
metadata, and a second copy would mean a second stale-run cleanup, a second
liveness guard and a second status route. Re-added NOT VALID for the same reason
a7c0d21e5f38 did — prod carries historical rows, and this only ever widens the
accepted set, so there is nothing a scan could usefully reject.

Revision ID: d5b81c47f9a3
Revises: b7d3e5c81f04
Create Date: 2026-08-10 12:00:00.000000+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d5b81c47f9a3"
down_revision: str | Sequence[str] | None = "b7d3e5c81f04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KINDS_WITHOUT_CATALOG_INITIAL = (
    "'initial', 'update', 'akas_backfill', 'ratings_backfill', "
    "'show_refresh', 'person_update', 'episode_credits_backfill'"
)
_KINDS_WITH_CATALOG_INITIAL = f"{_KINDS_WITHOUT_CATALOG_INITIAL}, 'catalog_initial'"


def upgrade() -> None:
    op.add_column(
        "show",
        sa.Column("tmdb_synced_at", sa.DateTime(timezone=True), nullable=True),
        schema="catalog",
    )
    op.execute("ALTER TABLE tvmaze.ingest_run DROP CONSTRAINT ck_ingest_run_kind")
    op.execute(
        "ALTER TABLE tvmaze.ingest_run ADD CONSTRAINT ck_ingest_run_kind "
        f"CHECK (kind IN ({_KINDS_WITH_CATALOG_INITIAL})) NOT VALID"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE tvmaze.ingest_run DROP CONSTRAINT ck_ingest_run_kind")
    op.execute(
        "ALTER TABLE tvmaze.ingest_run ADD CONSTRAINT ck_ingest_run_kind "
        f"CHECK (kind IN ({_KINDS_WITHOUT_CATALOG_INITIAL})) NOT VALID"
    )
    op.drop_column("show", "tmdb_synced_at", schema="catalog")
