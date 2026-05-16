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
    # Postgres 12+ supports `ALTER TYPE ... ADD VALUE` inside a transaction
    # provided the new value isn't *used* in the same transaction — and we
    # don't, so this runs on Alembic's own connection. Earlier versions of
    # this migration opened a separate AUTOCOMMIT connection from the pool;
    # that breaks on fresh databases (e.g. CI), because the CREATE TYPE from
    # the previous migration is still in Alembic's uncommitted transaction
    # and isn't visible to a new pool connection yet.
    op.execute("ALTER TYPE app.auth_token_purpose ADD VALUE IF NOT EXISTS 'email_change'")

    op.add_column(
        "auth_token",
        sa.Column("payload", postgresql.JSONB(), nullable=True),
        schema="app",
    )


def downgrade() -> None:
    op.drop_column("auth_token", "payload", schema="app")
    # Postgres doesn't support removing enum values; leave 'email_change' in place.
