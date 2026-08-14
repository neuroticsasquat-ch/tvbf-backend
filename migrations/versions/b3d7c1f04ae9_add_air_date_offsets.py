"""Correct mirrored airdates by a per-season offset (NEU-1145)

Three things, all of them one feature.

`catalog.air_date_offset` records how many days one season's dates are shifted
from TMDB's own. TMDB carries a single contributor-entered calendar date per
episode and no regional model at all — measured — so a season entered against
the US west coast records the Pacific day and reads a day early for everyone
else. The shift splits *within* a show, uniformly per season, which is why the
key is `(show_id, season_number)` and not the show.

The four `tmdb_*` date columns hold the uncorrected upstream value, and only
when a correction was applied. NULL therefore means "this row is untouched
TMDB", which is true of all but a handful of the ~6.0M dated episode rows.
They are what make the correction idempotent: every writer sets the pair
together from the raw value, so `corrected = raw + offset` is an invariant
rather than a history of edits, and re-running an ingest cannot double-apply a
shift. Un-doing the whole feature is one UPDATE per grain, which is what the
partial index on the episode grain is for.

`airdate_reconcile` joins the run-kind vocabulary, NOT VALID for the same
reason e3f16b90c2da and d5b81c47f9a3 were: prod carries historical run rows and
this only ever widens the accepted set, so there is nothing a validating scan
could usefully reject.

Revision ID: b3d7c1f04ae9
Revises: a7e3c8d15f42
Create Date: 2026-08-14 09:00:00.000000+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b3d7c1f04ae9"
down_revision: str | Sequence[str] | None = "a7e3c8d15f42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KINDS_WITHOUT_AIRDATE = (
    "'initial', 'update', 'akas_backfill', 'ratings_backfill', "
    "'show_refresh', 'person_update', 'episode_credits_backfill', "
    "'catalog_initial', 'catalog_update'"
)
_KINDS_WITH_AIRDATE = f"{_KINDS_WITHOUT_AIRDATE}, 'airdate_reconcile'"


def upgrade() -> None:
    op.create_table(
        "air_date_offset",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False, start=1), nullable=False),
        sa.Column("show_id", sa.BigInteger(), nullable=False),
        # NULL is a show-wide default that a numbered row overrides. The
        # reconciliation job never writes one; it is the operator's escape
        # hatch, which is what keeps the table's two writers distinguishable.
        sa.Column("season_number", sa.Integer(), nullable=True),
        sa.Column("offset_days", sa.Integer(), nullable=False),
        sa.Column("episodes_compared", sa.Integer(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["show_id"],
            ["catalog.show.id"],
            name=op.f("fk_air_date_offset_show_id_show"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_air_date_offset")),
        sa.CheckConstraint("offset_days IN (-1, 1)", name="ck_air_date_offset_days"),
        schema="catalog",
    )
    # NULLS NOT DISTINCT because the show-wide default *is* a NULL
    # `season_number`: under Postgres's default two of them would never
    # conflict, so an idempotent upsert would insert a second default on every
    # run and the override rule would stop being well defined.
    op.execute(
        "ALTER TABLE catalog.air_date_offset "
        "ADD CONSTRAINT uq_air_date_offset_show_season "
        "UNIQUE NULLS NOT DISTINCT (show_id, season_number)"
    )

    op.add_column("episode", sa.Column("tmdb_air_date", sa.Date(), nullable=True), schema="catalog")
    op.add_column("season", sa.Column("tmdb_air_date", sa.Date(), nullable=True), schema="catalog")
    op.add_column(
        "show", sa.Column("tmdb_first_air_date", sa.Date(), nullable=True), schema="catalog"
    )
    op.add_column(
        "show", sa.Column("tmdb_last_air_date", sa.Date(), nullable=True), schema="catalog"
    )

    # Partial, because the predicate it serves is "every row we altered" and
    # that is a rounding error against ~6.5M episodes.
    op.create_index(
        "ix_episode_tmdb_air_date_corrected",
        "episode",
        ["show_id"],
        unique=False,
        schema="catalog",
        postgresql_where=sa.text("tmdb_air_date IS NOT NULL"),
    )

    op.execute("ALTER TABLE catalog.ingest_run DROP CONSTRAINT ck_ingest_run_kind")
    op.execute(
        "ALTER TABLE catalog.ingest_run ADD CONSTRAINT ck_ingest_run_kind "
        f"CHECK (kind IN ({_KINDS_WITH_AIRDATE})) NOT VALID"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE catalog.ingest_run DROP CONSTRAINT ck_ingest_run_kind")
    op.execute(
        "ALTER TABLE catalog.ingest_run ADD CONSTRAINT ck_ingest_run_kind "
        f"CHECK (kind IN ({_KINDS_WITHOUT_AIRDATE})) NOT VALID"
    )

    # Restore the upstream values before the columns holding them go. Dropping
    # them first would strand every corrected row on a date TMDB never sent,
    # with nothing left to say so.
    op.execute(
        "UPDATE catalog.episode SET air_date = tmdb_air_date WHERE tmdb_air_date IS NOT NULL"
    )
    op.execute("UPDATE catalog.season SET air_date = tmdb_air_date WHERE tmdb_air_date IS NOT NULL")
    op.execute(
        "UPDATE catalog.show SET first_air_date = tmdb_first_air_date "
        "WHERE tmdb_first_air_date IS NOT NULL"
    )
    op.execute(
        "UPDATE catalog.show SET last_air_date = tmdb_last_air_date "
        "WHERE tmdb_last_air_date IS NOT NULL"
    )

    op.drop_index("ix_episode_tmdb_air_date_corrected", table_name="episode", schema="catalog")
    op.drop_column("show", "tmdb_last_air_date", schema="catalog")
    op.drop_column("show", "tmdb_first_air_date", schema="catalog")
    op.drop_column("season", "tmdb_air_date", schema="catalog")
    op.drop_column("episode", "tmdb_air_date", schema="catalog")
    op.drop_table("air_date_offset", schema="catalog")
