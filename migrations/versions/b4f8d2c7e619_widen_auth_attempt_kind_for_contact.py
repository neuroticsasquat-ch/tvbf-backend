"""Widen ck_auth_attempt_kind to include 'contact' (NEU-1164).

The contact form reuses the same IP-keyed throttle mechanism that signup and
login use, so the vocabulary must grow to admit kind='contact'.

Revision ID: b4f8d2c7e619
Revises: 15c966ca569e
Create Date: 2026-08-21 00:00:00.000000+00:00

"""

from alembic import op

revision = "b4f8d2c7e619"
down_revision = "15c966ca569e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE app.auth_attempt DROP CONSTRAINT IF EXISTS ck_auth_attempt_kind"
    )
    op.create_check_constraint(
        "ck_auth_attempt_kind",
        "auth_attempt",
        "kind IN ('signup', 'login', 'contact')",
        schema="app",
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE app.auth_attempt DROP CONSTRAINT IF EXISTS ck_auth_attempt_kind"
    )
    op.create_check_constraint(
        "ck_auth_attempt_kind",
        "auth_attempt",
        "kind IN ('signup', 'login')",
        schema="app",
    )
