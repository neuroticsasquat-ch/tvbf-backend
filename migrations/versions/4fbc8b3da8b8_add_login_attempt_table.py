"""add login_attempt table

Revision ID: 4fbc8b3da8b8
Revises: da118e6837e0
Create Date: 2026-04-26 16:36:00.530442+00:00

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "4fbc8b3da8b8"
down_revision = "da118e6837e0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "login_attempt",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("email", postgresql.CITEXT(), nullable=False),
        sa.Column(
            "attempted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("ip", postgresql.INET(), nullable=True),
        schema="app",
    )
    op.create_index(
        "ix_login_attempt_email_at",
        "login_attempt",
        ["email", "attempted_at"],
        schema="app",
    )


def downgrade() -> None:
    op.drop_index("ix_login_attempt_email_at", table_name="login_attempt", schema="app")
    op.drop_table("login_attempt", schema="app")
