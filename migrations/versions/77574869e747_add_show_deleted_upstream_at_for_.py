"""add show.deleted_upstream_at for tombstoning (NEU-1005)

Revision ID: 77574869e747
Revises: d41c8a6b7e92
Create Date: 2026-08-06 22:10:05.910539+00:00

Autogenerate also proposed dropping seven indexes -- ix_episode_show_id_season,
ix_person_name_folded_trgm, ix_season_show_id, ix_show_name_folded_trgm,
ix_show_name_trgm, ix_show_aka_name_trgm, ix_show_aka_show_id. They exist in
the database but are not declared on the models, so autogenerate reads them as
removed. They are load-bearing -- the trigram ones are what make AKA-aware
search not table-scan 89k shows -- and have been stripped. Only the column
change belongs in this migration.
"""

import sqlalchemy as sa
from alembic import op

revision = "77574869e747"
down_revision = "d41c8a6b7e92"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "show",
        sa.Column("deleted_upstream_at", sa.DateTime(timezone=True), nullable=True),
        schema="tvmaze",
    )


def downgrade() -> None:
    op.drop_column("show", "deleted_upstream_at", schema="tvmaze")
