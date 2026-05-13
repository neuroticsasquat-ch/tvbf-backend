"""add app.user.email_verified_at

Revision ID: 7e2b91a4c5d3
Revises: 3a7c5e9b4f10
Create Date: 2026-05-13 00:00:00.000000+00:00

"""
import sqlalchemy as sa
from alembic import op


revision = "7e2b91a4c5d3"
down_revision = "3a7c5e9b4f10"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user",
        sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True),
        schema="app",
    )


def downgrade() -> None:
    op.drop_column("user", "email_verified_at", schema="app")
