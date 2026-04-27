"""index episode(show_id, season) and season(show_id)

Revision ID: c1a2f3d4e5b6
Revises: f8ceecec4809
Create Date: 2026-04-27 00:00:00.000000+00:00

The /shows/{id}/episodes endpoint filters episodes by (show_id, season). With
~3.4M rows in tvmaze.episode and no index on show_id, every request was a full
parallel seq scan — fast enough locally, but 5-10s on Azure's shared instance.
Adding a btree on (show_id, season) turns it into an index scan. Same idea for
season(show_id), used by /shows/{id}/seasons and the show-detail join.

CREATE INDEX CONCURRENTLY so the index build doesn't block ingest writes.
"""

from alembic import op

revision = "c1a2f3d4e5b6"
down_revision = "f8ceecec4809"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_episode_show_id_season "
            "ON tvmaze.episode (show_id, season)"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_season_show_id "
            "ON tvmaze.season (show_id)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS tvmaze.ix_season_show_id")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS tvmaze.ix_episode_show_id_season")
