"""add user is_admin column

Revision ID: c71bc6a53424
Revises: 2e6f84ab23fd
Create Date: 2026-05-15 23:05:00.000000+00:00

"""
import sqlalchemy as sa
from alembic import op

revision = 'c71bc6a53424'
down_revision = '2e6f84ab23fd'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user",
        sa.Column(
            "is_admin",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("FALSE"),
        ),
        schema="app",
    )


def downgrade() -> None:
    op.drop_column("user", "is_admin", schema="app")
