"""add catalog.show_event and the push_deliver run kind (NEU-1480)

The append-only catalog-event sidecar (Push Notifications project spec §4.1,
ADR-0014). The daily delta writes one row per transition it sees on a tracked
show — a premiere date set or moved, a show ended or revived — in the show's
own transaction; the push delivery job reads them. No uniqueness: a date that
moves twice is two rows, and delivery keys on `id`.

`ix_show_event_show_id_kind` leads on `show_id`, so it also serves the CASCADE
from `catalog.show`. `ix_show_event_season_id` exists only for the CASCADE from
`catalog.season` — the delta prunes seasons upstream stops listing, and each
prune would otherwise scan this table. It is partial because the two status
kinds never carry a season.

`push_deliver` joins the run-kind vocabulary, NOT VALID for the same reason
b3d7c1f04ae9, e3f16b90c2da and c9a1f0b7d213 were: prod carries historical run
rows and this only ever widens the accepted set, so there is nothing a
validating scan could usefully reject.

Revision ID: a4e9c7d2b813
Revises: 434b11ea2bda
Create Date: 2026-09-26 22:00:00.000000+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a4e9c7d2b813"
down_revision: str | Sequence[str] | None = "434b11ea2bda"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KINDS_WITHOUT_PUSH = (
    "'initial', 'update', 'akas_backfill', 'ratings_backfill', "
    "'show_refresh', 'person_update', 'episode_credits_backfill', "
    "'catalog_initial', 'catalog_update', 'airdate_reconcile', "
    "'trending_snapshot'"
)
_KINDS_WITH_PUSH = f"{_KINDS_WITHOUT_PUSH}, 'push_deliver'"


def upgrade() -> None:
    op.create_table(
        "show_event",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("show_id", sa.BigInteger(), nullable=False),
        sa.Column("season_id", sa.BigInteger(), nullable=True),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=True),
        sa.Column(
            "observed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "kind IN ('premiere_set', 'premiere_moved', 'ended', 'revived')",
            name="ck_show_event_kind",
        ),
        sa.ForeignKeyConstraint(["show_id"], ["catalog.show.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["season_id"], ["catalog.season.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["catalog.ingest_run.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        schema="catalog",
    )
    op.create_index("ix_show_event_observed_at", "show_event", ["observed_at"], schema="catalog")
    op.create_index(
        "ix_show_event_show_id_kind", "show_event", ["show_id", "kind"], schema="catalog"
    )
    op.create_index(
        "ix_show_event_season_id",
        "show_event",
        ["season_id"],
        schema="catalog",
        postgresql_where=sa.text("season_id IS NOT NULL"),
    )

    op.execute("ALTER TABLE catalog.ingest_run DROP CONSTRAINT ck_ingest_run_kind")
    op.execute(
        "ALTER TABLE catalog.ingest_run ADD CONSTRAINT ck_ingest_run_kind "
        f"CHECK (kind IN ({_KINDS_WITH_PUSH})) NOT VALID"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE catalog.ingest_run DROP CONSTRAINT ck_ingest_run_kind")
    op.execute(
        "ALTER TABLE catalog.ingest_run ADD CONSTRAINT ck_ingest_run_kind "
        f"CHECK (kind IN ({_KINDS_WITHOUT_PUSH})) NOT VALID"
    )
    op.drop_table("show_event", schema="catalog")
