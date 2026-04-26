"""drop user_show_watch status column

Revision ID: da118e6837e0
Revises: a213dee9e56f
Create Date: 2026-04-26 00:44:09.672324+00:00

"""

import sqlalchemy as sa
from alembic import op

revision = "da118e6837e0"
down_revision = "a213dee9e56f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Tracking presence in My Shows replaces the old status enum. Rows with
    # status='dropped' represented "no longer wants this show" — equivalent
    # to removal under the new model, so drop them before removing the column.
    op.execute("DELETE FROM app.user_show_watch WHERE status = 'dropped'")
    op.drop_index(
        "ix_user_show_watch_user_status",
        table_name="user_show_watch",
        schema="app",
    )
    op.drop_constraint(
        "ck_user_show_watch_status",
        "user_show_watch",
        schema="app",
        type_="check",
    )
    op.drop_column("user_show_watch", "status", schema="app")


def downgrade() -> None:
    op.add_column(
        "user_show_watch",
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default="watching",
        ),
        schema="app",
    )
    op.alter_column("user_show_watch", "status", server_default=None, schema="app")
    op.create_check_constraint(
        "ck_user_show_watch_status",
        "user_show_watch",
        "status IN ('watching','want_to_watch','dropped')",
        schema="app",
    )
    op.create_index(
        "ix_user_show_watch_user_status",
        "user_show_watch",
        ["user_id", "status"],
        schema="app",
    )
