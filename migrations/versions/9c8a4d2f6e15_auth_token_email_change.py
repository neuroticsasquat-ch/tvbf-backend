"""extend auth_token: add email_change purpose + payload column

Revision ID: 9c8a4d2f6e15
Revises: 7e2b91a4c5d3
Create Date: 2026-05-13 00:00:00.000000+00:00

"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "9c8a4d2f6e15"
down_revision = "7e2b91a4c5d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Postgres requires ALTER TYPE ... ADD VALUE outside a transaction block.
    # Alembic runs each migration in its own transaction; we work around that
    # by running the ADD VALUE on an AUTOCOMMIT connection.
    bind = op.get_bind()
    with bind.engine.connect() as conn:
        conn = conn.execution_options(isolation_level="AUTOCOMMIT")
        conn.execute(
            sa.text("ALTER TYPE app.auth_token_purpose ADD VALUE IF NOT EXISTS 'email_change'")
        )

    op.add_column(
        "auth_token",
        sa.Column("payload", postgresql.JSONB(), nullable=True),
        schema="app",
    )


def downgrade() -> None:
    op.drop_column("auth_token", "payload", schema="app")
    # Postgres doesn't support removing enum values; leave 'email_change' in place.
