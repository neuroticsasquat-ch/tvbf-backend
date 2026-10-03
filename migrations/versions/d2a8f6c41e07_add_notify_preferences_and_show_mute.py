"""add notify_* preferences on app.user and muted on app.user_show_watch (NEU-1490)

Push Notifications project spec §4.4. Five per-kind opt-outs, default on, and a
per-show mute that silences every kind for that show, default off. `muted`
rides beside `hide_from_activity` and is toggled the same way.

Revision ID: d2a8f6c41e07
Revises: b7d3e5a91c40
Create Date: 2026-09-26 23:45:00.000000+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d2a8f6c41e07"
down_revision: str | Sequence[str] | None = "b7d3e5a91c40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NOTIFY_COLUMNS = (
    "notify_airs_today",
    "notify_premiere_set",
    "notify_premiere_moved",
    "notify_ended",
    "notify_revived",
)


def upgrade() -> None:
    for name in NOTIFY_COLUMNS:
        op.add_column(
            "user",
            sa.Column(name, sa.Boolean(), nullable=False, server_default=sa.text("TRUE")),
            schema="app",
        )
    op.add_column(
        "user_show_watch",
        sa.Column("muted", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")),
        schema="app",
    )


def downgrade() -> None:
    op.drop_column("user_show_watch", "muted", schema="app")
    for name in reversed(NOTIFY_COLUMNS):
        op.drop_column("user", name, schema="app")
