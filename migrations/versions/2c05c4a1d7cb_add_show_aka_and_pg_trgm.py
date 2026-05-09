"""'add show_aka and pg_trgm'

Revision ID: 2c05c4a1d7cb
Revises: c1a2f3d4e5b6
Create Date: 2026-05-03 21:12:51.389692+00:00

"""
from alembic import op
import sqlalchemy as sa


revision = '2c05c4a1d7cb'
down_revision = 'c1a2f3d4e5b6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. pg_trgm extension (idempotent).
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # 2. Trigram index on show.name for fast ILIKE substring matching.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_show_name_trgm "
        "ON tvmaze.show USING gin (name gin_trgm_ops)"
    )

    # 3. show.akas_synced_at — NULL means "not yet synced".
    op.add_column(
        "show",
        sa.Column("akas_synced_at", sa.DateTime(timezone=True), nullable=True),
        schema="tvmaze",
    )

    # 4. show_aka table.
    op.create_table(
        "show_aka",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "show_id",
            sa.Integer(),
            sa.ForeignKey("tvmaze.show.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("country_code", sa.Text(), nullable=True),
        sa.Column("country_name", sa.Text(), nullable=True),
        sa.Column("language", sa.Text(), nullable=True),
        schema="tvmaze",
    )
    op.create_index(
        "ix_show_aka_show_id",
        "show_aka",
        ["show_id"],
        schema="tvmaze",
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_show_aka_name_trgm "
        "ON tvmaze.show_aka USING gin (name gin_trgm_ops)"
    )

    # 5. Expand ingest_run.kind to include 'akas_backfill'.
    op.execute("ALTER TABLE tvmaze.ingest_run DROP CONSTRAINT ck_ingest_run_kind")
    op.execute(
        "ALTER TABLE tvmaze.ingest_run ADD CONSTRAINT ck_ingest_run_kind "
        "CHECK (kind IN ('initial', 'update', 'akas_backfill'))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE tvmaze.ingest_run DROP CONSTRAINT ck_ingest_run_kind")
    op.execute(
        "ALTER TABLE tvmaze.ingest_run ADD CONSTRAINT ck_ingest_run_kind "
        "CHECK (kind IN ('initial', 'update'))"
    )

    op.drop_index("ix_show_aka_name_trgm", table_name="show_aka", schema="tvmaze")
    op.drop_index("ix_show_aka_show_id", table_name="show_aka", schema="tvmaze")
    op.drop_table("show_aka", schema="tvmaze")

    op.drop_column("show", "akas_synced_at", schema="tvmaze")

    # Do NOT drop ix_show_name_trgm — it is owned by migration e5867a694d27.
    # This migration's upgrade uses CREATE INDEX IF NOT EXISTS purely as a
    # safety net; the index belongs to the prior migration on downgrade too.
    # Leave pg_trgm installed; other migrations may rely on it.
