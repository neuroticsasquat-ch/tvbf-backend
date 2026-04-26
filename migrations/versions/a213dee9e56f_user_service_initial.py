"""user service initial

Revision ID: a213dee9e56f
Revises: 2668ec5f731e
Create Date: 2026-04-25 21:58:57.764675+00:00

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "a213dee9e56f"
down_revision = "2668ec5f731e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    op.create_table(
        "user",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("email", postgresql.CITEXT(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("email", name="uq_user_email"),
        schema="app",
    )

    op.create_table(
        "session",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("ip", postgresql.INET(), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"], ["app.user.id"], ondelete="CASCADE", name="fk_session_user"
        ),
        schema="app",
    )
    op.create_index("ix_session_user_id", "session", ["user_id"], schema="app")
    op.create_index("ix_session_expires_at", "session", ["expires_at"], schema="app")

    op.create_table(
        "user_show_watch",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("show_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("user_id", "show_id"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["app.user.id"], ondelete="CASCADE", name="fk_usw_user"
        ),
        sa.ForeignKeyConstraint(
            ["show_id"], ["tvmaze.show.id"], ondelete="CASCADE", name="fk_usw_show"
        ),
        sa.CheckConstraint(
            "status IN ('watching','want_to_watch','dropped')",
            name="ck_user_show_watch_status",
        ),
        schema="app",
    )
    op.create_index(
        "ix_user_show_watch_user_status",
        "user_show_watch",
        ["user_id", "status"],
        schema="app",
    )

    op.create_table(
        "user_episode_watch",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("episode_id", sa.Integer(), nullable=False),
        sa.Column(
            "watched_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("user_id", "episode_id"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["app.user.id"], ondelete="CASCADE", name="fk_uew_user"
        ),
        sa.ForeignKeyConstraint(
            ["episode_id"],
            ["tvmaze.episode.id"],
            ondelete="CASCADE",
            name="fk_uew_episode",
        ),
        schema="app",
    )


def downgrade() -> None:
    op.drop_table("user_episode_watch", schema="app")
    op.drop_table("user_show_watch", schema="app")
    op.drop_index("ix_session_expires_at", table_name="session", schema="app")
    op.drop_index("ix_session_user_id", table_name="session", schema="app")
    op.drop_table("session", schema="app")
    op.drop_table("user", schema="app")
