"""add the push_airs_today and push_events run kinds (NEU-1540)

The single daily push delivery splits into two scheduled tasks, each with its
own run kind so the per-kind in-flight guard never lets one block the other.
`push_deliver` stays in the list: prod carries its historical run rows, and the
retired job writes no new ones. NOT VALID for the reason a4e9c7d2b813 gives —
this only ever widens the accepted set, so there is nothing a validating scan
could usefully reject.

Revision ID: c5f2a8e1d940
Revises: a964646910d2
Create Date: 2026-10-08 12:00:00.000000+00:00

"""

from collections.abc import Sequence

from alembic import op

revision: str = "c5f2a8e1d940"
down_revision: str | Sequence[str] | None = "a964646910d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KINDS_BEFORE = (
    "'initial', 'update', 'akas_backfill', 'ratings_backfill', "
    "'show_refresh', 'person_update', 'episode_credits_backfill', "
    "'catalog_initial', 'catalog_update', 'airdate_reconcile', "
    "'trending_snapshot', 'push_deliver'"
)
_KINDS_AFTER = f"{_KINDS_BEFORE}, 'push_airs_today', 'push_events'"


def upgrade() -> None:
    op.execute("ALTER TABLE catalog.ingest_run DROP CONSTRAINT ck_ingest_run_kind")
    op.execute(
        "ALTER TABLE catalog.ingest_run ADD CONSTRAINT ck_ingest_run_kind "
        f"CHECK (kind IN ({_KINDS_AFTER})) NOT VALID"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE catalog.ingest_run DROP CONSTRAINT ck_ingest_run_kind")
    op.execute(
        "ALTER TABLE catalog.ingest_run ADD CONSTRAINT ck_ingest_run_kind "
        f"CHECK (kind IN ({_KINDS_BEFORE})) NOT VALID"
    )
