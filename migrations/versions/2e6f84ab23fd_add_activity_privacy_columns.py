"""add activity privacy columns

Revision ID: 2e6f84ab23fd
Revises: e0ae79467fa2
Create Date: 2026-05-15 22:20:00.000000+00:00

"""
from alembic import op
import sqlalchemy as sa


revision = '2e6f84ab23fd'
down_revision = 'e0ae79467fa2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user",
        sa.Column(
            "activity_feed_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("TRUE"),
        ),
        schema="app",
    )
    op.add_column(
        "user_show_watch",
        sa.Column(
            "hide_from_activity",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("FALSE"),
        ),
        schema="app",
    )


def downgrade() -> None:
    op.drop_column("user_show_watch", "hide_from_activity", schema="app")
    op.drop_column("user", "activity_feed_enabled", schema="app")
