"""'add ratings'

Revision ID: 68afd4fae961
Revises: 9c8a4d2f6e15
Create Date: 2026-05-14 20:17:53.256049+00:00

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = '68afd4fae961'
down_revision = '9c8a4d2f6e15'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. tvmaze columns
    op.add_column(
        "show",
        sa.Column("rating_average", sa.Numeric(3, 1), nullable=True),
        schema="tvmaze",
    )
    op.add_column(
        "show",
        sa.Column("ratings_synced_at", sa.DateTime(timezone=True), nullable=True),
        schema="tvmaze",
    )
    op.add_column(
        "episode",
        sa.Column("rating_average", sa.Numeric(3, 1), nullable=True),
        schema="tvmaze",
    )

    # 2. Expand ingest_run.kind check constraint to include 'ratings_backfill'
    op.execute("ALTER TABLE tvmaze.ingest_run DROP CONSTRAINT ck_ingest_run_kind")
    op.execute(
        "ALTER TABLE tvmaze.ingest_run ADD CONSTRAINT ck_ingest_run_kind "
        "CHECK (kind IN ('initial', 'update', 'akas_backfill', 'ratings_backfill'))"
    )

    # 3. app.user_show_rating
    op.create_table(
        "user_show_rating",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("app.user.id", ondelete="CASCADE", name="fk_user_show_rating_user"),
            nullable=False,
        ),
        sa.Column(
            "show_id",
            sa.BigInteger(),
            sa.ForeignKey("tvmaze.show.id", ondelete="CASCADE", name="fk_user_show_rating_show"),
            nullable=False,
        ),
        sa.Column("stars", sa.Numeric(2, 1), nullable=False),
        sa.Column(
            "rated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "stars IN (0.5,1.0,1.5,2.0,2.5,3.0,3.5,4.0,4.5,5.0)",
            name="ck_user_show_rating_stars",
        ),
        sa.UniqueConstraint("user_id", "show_id", name="uq_user_show_rating"),
        schema="app",
    )
    op.create_index(
        "ix_user_show_rating_user_id", "user_show_rating", ["user_id"], schema="app"
    )
    op.create_index(
        "ix_user_show_rating_show_id", "user_show_rating", ["show_id"], schema="app"
    )

    # 4. app.user_episode_rating
    op.create_table(
        "user_episode_rating",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("app.user.id", ondelete="CASCADE", name="fk_user_episode_rating_user"),
            nullable=False,
        ),
        sa.Column(
            "episode_id",
            sa.BigInteger(),
            sa.ForeignKey("tvmaze.episode.id", ondelete="CASCADE", name="fk_user_episode_rating_episode"),
            nullable=False,
        ),
        sa.Column("stars", sa.Numeric(2, 1), nullable=False),
        sa.Column(
            "rated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "stars IN (0.5,1.0,1.5,2.0,2.5,3.0,3.5,4.0,4.5,5.0)",
            name="ck_user_episode_rating_stars",
        ),
        sa.UniqueConstraint("user_id", "episode_id", name="uq_user_episode_rating"),
        schema="app",
    )
    op.create_index(
        "ix_user_episode_rating_user_id", "user_episode_rating", ["user_id"], schema="app"
    )
    op.create_index(
        "ix_user_episode_rating_episode_id",
        "user_episode_rating",
        ["episode_id"],
        schema="app",
    )


def downgrade() -> None:
    op.drop_index("ix_user_episode_rating_episode_id", table_name="user_episode_rating", schema="app")
    op.drop_index("ix_user_episode_rating_user_id", table_name="user_episode_rating", schema="app")
    op.drop_table("user_episode_rating", schema="app")
    op.drop_index("ix_user_show_rating_show_id", table_name="user_show_rating", schema="app")
    op.drop_index("ix_user_show_rating_user_id", table_name="user_show_rating", schema="app")
    op.drop_table("user_show_rating", schema="app")

    op.execute("ALTER TABLE tvmaze.ingest_run DROP CONSTRAINT ck_ingest_run_kind")
    op.execute(
        "ALTER TABLE tvmaze.ingest_run ADD CONSTRAINT ck_ingest_run_kind "
        "CHECK (kind IN ('initial', 'update', 'akas_backfill'))"
    )

    op.drop_column("episode", "rating_average", schema="tvmaze")
    op.drop_column("show", "ratings_synced_at", schema="tvmaze")
    op.drop_column("show", "rating_average", schema="tvmaze")
