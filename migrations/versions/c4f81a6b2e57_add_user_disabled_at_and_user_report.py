"""Add app.user.disabled_at and app.user_report — the moderation surface

NEU-1162. There is no moderation capability: `routers/admin_users.py` toggles
`is_admin` and nothing else, so the only remedy against an abusive account is
the irreversible `DELETE /me` shape, which destroys the person's watch history
and which nobody will therefore reach for.

`disabled_at` is a timestamp rather than a boolean because the question asked
of a moderation action later is *when*. It carries no `disabled_by` and no
`disabled_reason` (spec §1.1): there is one admin, and the grounds live in the
report row and the Linear issue it created — both are additive later if
moderation ever grows past one person.

`user_report` is the report ledger *and* the throttle's counter — every report
is persisted anyway, so there is no `auth_attempt`-style side table to keep in
step. Both FKs cascade, consistent with every other FK into `app.user`: an
abuser who self-deletes takes their reports with them, and the Linear issue is
the durable record. `RESTRICT` on `reported_user_id` would have let anyone block
another person's account deletion by reporting them.

Pruning is out of scope, the same standing gap `app.auth_attempt` has: the
throttle bounds accrual at 5 rows per reporter per day.

Revision ID: c4f81a6b2e57
Revises: e2a8c14f70b3
Create Date: 2026-08-19 21:00:00.000000+00:00

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "c4f81a6b2e57"
down_revision = "e2a8c14f70b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user",
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        schema="app",
    )
    op.create_table(
        "user_report",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("reporter_id", UUID(as_uuid=True), nullable=False),
        sa.Column("reported_user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["reporter_id"],
            ["app.user.id"],
            name="fk_user_report_reporter",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["reported_user_id"],
            ["app.user.id"],
            name="fk_user_report_reported_user",
            ondelete="CASCADE",
        ),
        schema="app",
    )
    # The throttle's exact query: count one reporter's rows since a timestamp.
    op.create_index(
        "ix_user_report_reporter_created",
        "user_report",
        ["reporter_id", "created_at"],
        schema="app",
    )


def downgrade() -> None:
    op.drop_index("ix_user_report_reporter_created", table_name="user_report", schema="app")
    op.drop_table("user_report", schema="app")
    op.drop_column("user", "disabled_at", schema="app")
