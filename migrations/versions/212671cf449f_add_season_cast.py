"""add catalog.season_cast and catalog.show.season_credits_synced_at (NEU-1512)

A season's regular cast, from TMDB's `season/{n}/credits.cast[]` — upstream's
only record of who was a regular in which season. Until now a regular existed in
the catalog once, as a `show_cast` row with no season or episode attached, so
nothing could put them on a season page and the show page could not tell them
from a guest.

`uq_season_cast_season_person_character` is `NULLS NOT DISTINCT` for
`uq_egc_episode_person_character`'s reason: `character_id` is nullable, and
two null-character rows for one person on one season must conflict or every
re-ingest duplicates them. It also leads with `season_id`, so it carries the
per-season lookup and the cascade from `catalog.season`; `ix_season_cast_person_id`
is for the person page.

`season_credits_synced_at` is the fourth watermark on `catalog.show`, by the
rule `recommendations_synced_at` made one: "the show has no `season_cast` row"
cannot tell *upstream lists no regulars* from *nobody has asked*. Nullable with
no backfill and no index — NULL is right for every existing row, and
`credits_synced_at` ran the same work-list predicate at catalog scale unindexed.

No `ingest_run` kind: the backfill that fills this writes no run row, like the
credits backfill it copies.

Revision ID: 212671cf449f
Revises: 28e392fdeb47
Create Date: 2026-09-30 18:00:00.000000+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "212671cf449f"
down_revision: str | Sequence[str] | None = "28e392fdeb47"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "show",
        sa.Column("season_credits_synced_at", sa.DateTime(timezone=True), nullable=True),
        schema="catalog",
    )
    op.create_table(
        "season_cast",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False, start=1), nullable=False),
        sa.Column("season_id", sa.BigInteger(), nullable=False),
        sa.Column("person_id", sa.BigInteger(), nullable=False),
        sa.Column("character_id", sa.BigInteger(), nullable=True),
        sa.Column("credit_id", sa.Text(), nullable=True),
        sa.Column("billing_order", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["season_id"], ["catalog.season.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["person_id"], ["catalog.person.id"]),
        sa.ForeignKeyConstraint(["character_id"], ["catalog.character.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "season_id",
            "person_id",
            "character_id",
            name="uq_season_cast_season_person_character",
            postgresql_nulls_not_distinct=True,
        ),
        schema="catalog",
    )
    op.create_index(
        "ix_season_cast_person_id", "season_cast", ["person_id"], unique=False, schema="catalog"
    )


def downgrade() -> None:
    op.drop_index("ix_season_cast_person_id", table_name="season_cast", schema="catalog")
    op.drop_table("season_cast", schema="catalog")
    op.drop_column("show", "season_credits_synced_at", schema="catalog")
