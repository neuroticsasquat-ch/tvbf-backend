"""admit catalog_update as an ingest_run kind (NEU-1035)

The daily TMDB catalog delta's run kind. It is also the kind whose cursor
lineage stores a *date* — the end of the last `/tv/changes` window covered, as
the epoch of its midnight UTC — where every other lineage stores a TV Maze
last-modified epoch. Nothing about that needs a column: `last_update_cursor` is
already read scoped by kind (`get_last_successful_cursor`), precisely so one
ingest axis cannot resume from another's watermark.

Re-added NOT VALID for the same reason d5b81c47f9a3 and a7c0d21e5f38 were: prod
carries historical run rows, and this only ever widens the accepted set, so
there is nothing a validating scan could usefully reject.

Revision ID: e3f16b90c2da
Revises: d5b81c47f9a3
Create Date: 2026-08-10 18:00:00.000000+00:00

"""

from collections.abc import Sequence

from alembic import op

revision: str = "e3f16b90c2da"
down_revision: str | Sequence[str] | None = "d5b81c47f9a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KINDS_WITHOUT_CATALOG_UPDATE = (
    "'initial', 'update', 'akas_backfill', 'ratings_backfill', "
    "'show_refresh', 'person_update', 'episode_credits_backfill', 'catalog_initial'"
)
_KINDS_WITH_CATALOG_UPDATE = f"{_KINDS_WITHOUT_CATALOG_UPDATE}, 'catalog_update'"


def upgrade() -> None:
    op.execute("ALTER TABLE tvmaze.ingest_run DROP CONSTRAINT ck_ingest_run_kind")
    op.execute(
        "ALTER TABLE tvmaze.ingest_run ADD CONSTRAINT ck_ingest_run_kind "
        f"CHECK (kind IN ({_KINDS_WITH_CATALOG_UPDATE})) NOT VALID"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE tvmaze.ingest_run DROP CONSTRAINT ck_ingest_run_kind")
    op.execute(
        "ALTER TABLE tvmaze.ingest_run ADD CONSTRAINT ck_ingest_run_kind "
        f"CHECK (kind IN ({_KINDS_WITHOUT_CATALOG_UPDATE})) NOT VALID"
    )
