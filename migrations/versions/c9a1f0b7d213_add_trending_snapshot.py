"""add catalog.trending_show and the trending_snapshot run kind (NEU-1055)

The trending surface's storage. One whole `/trending/tv/week` list, replaced
daily inside one transaction — twenty rows at most, all carrying the same
`captured_at`, which is when the list was *fetched* rather than when it was
written. NEU-1056's seven-day staleness cutoff is measured against that column,
which is the only reason it exists rather than being inferred from a run row.

`rank` is the primary key because the table holds exactly one snapshot: two rows
cannot share a position, and a surrogate id would only make that expressible.
Ranks may have gaps — an entry that does not resolve to a mirrored show is
dropped rather than renumbered, so the number keeps meaning TMDB's position.

`uq_trending_show_show` is a data statement and an index at once: it makes "the
writer deduplicates" an invariant, and it is what stops the CASCADE below
sequentially scanning the table on every show deletion.

`trending_snapshot` joins the run-kind vocabulary, NOT VALID for the same reason
b3d7c1f04ae9 and e3f16b90c2da were: prod carries historical run rows and this
only ever widens the accepted set, so there is nothing a validating scan could
usefully reject.

Revision ID: c9a1f0b7d213
Revises: f3a71c9d24b8
Create Date: 2026-08-16 18:00:00.000000+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c9a1f0b7d213"
down_revision: str | Sequence[str] | None = "f3a71c9d24b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KINDS_WITHOUT_TRENDING = (
    "'initial', 'update', 'akas_backfill', 'ratings_backfill', "
    "'show_refresh', 'person_update', 'episode_credits_backfill', "
    "'catalog_initial', 'catalog_update', 'airdate_reconcile'"
)
_KINDS_WITH_TRENDING = f"{_KINDS_WITHOUT_TRENDING}, 'trending_snapshot'"


def upgrade() -> None:
    op.create_table(
        "trending_show",
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("show_id", sa.BigInteger(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["show_id"], ["catalog.show.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("rank", name="pk_trending_show"),
        sa.UniqueConstraint("show_id", name="uq_trending_show_show"),
        schema="catalog",
    )

    op.execute("ALTER TABLE catalog.ingest_run DROP CONSTRAINT ck_ingest_run_kind")
    op.execute(
        "ALTER TABLE catalog.ingest_run ADD CONSTRAINT ck_ingest_run_kind "
        f"CHECK (kind IN ({_KINDS_WITH_TRENDING})) NOT VALID"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE catalog.ingest_run DROP CONSTRAINT ck_ingest_run_kind")
    op.execute(
        "ALTER TABLE catalog.ingest_run ADD CONSTRAINT ck_ingest_run_kind "
        f"CHECK (kind IN ({_KINDS_WITHOUT_TRENDING})) NOT VALID"
    )
    op.drop_table("trending_show", schema="catalog")
