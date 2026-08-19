"""Add app.auth_attempt — the IP-keyed signup/login throttle's table

NEU-1160. The existing lockout in `account_service.authenticate` is keyed on
**email only**, so credential stuffing that walks a different address on every
request is entirely unthrottled. This table is the counter that closes that,
and it also throttles signup, which today has no abuse protection beyond the
invite code NEU-1156 removes.

A new table rather than a widening of `app.login_attempt`: that table belongs
to the email-keyed lockout, its rows are cleared per-email on a successful
login, and the IP counter must deliberately *not* be cleared on success — an
attacker owns at least one valid account (their own), so clear-on-success would
hand them a reset button between every ten guesses.

Pruning is out of scope and the arithmetic is why: rows accrue at the rate of
failed logins plus signups, bounded above by the throttle itself at roughly
1,080 rows per attacking address per day, and `app.login_attempt` has carried
the same unbounded shape since it shipped.

Revision ID: e2a8c14f70b3
Revises: b7d3e02c9a41
Create Date: 2026-08-19 20:00:00.000000+00:00

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import INET

revision = "e2a8c14f70b3"
down_revision = "b7d3e02c9a41"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "auth_attempt",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("ip", INET(), nullable=False),
        sa.Column(
            "attempted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("kind IN ('signup', 'login')", name="ck_auth_attempt_kind"),
        schema="app",
    )
    # Leads (kind, ip, attempted_at) because every read is "count rows of one
    # kind for one address since a timestamp".
    op.create_index(
        "ix_auth_attempt_kind_ip_at",
        "auth_attempt",
        ["kind", "ip", "attempted_at"],
        schema="app",
    )


def downgrade() -> None:
    op.drop_index("ix_auth_attempt_kind_ip_at", table_name="auth_attempt", schema="app")
    op.drop_table("auth_attempt", schema="app")
