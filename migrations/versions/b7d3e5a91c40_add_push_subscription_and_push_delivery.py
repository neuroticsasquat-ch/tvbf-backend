"""add app.push_subscription and app.push_delivery (NEU-1485)

Push Notifications project spec §4.2 and §4.3.

`push_subscription` is one browser's Web Push subscription. `endpoint` is
unique because an endpoint belongs to exactly one user: re-subscribing with one
that already exists upserts the row onto whoever is logged in now.

`push_delivery` is the delivery log and the idempotency rule in one — unique
`(notification_key, subscription_id)`, with a `pending` row inserted before the
send. **`subscription_id` is SET NULL, not CASCADE**: retiring a subscription
deletes it, and the `failed` row that recorded why must survive for
`GET /admin/push/stats` to count retirements. NULLs are distinct in the unique
index, so orphaned rows never collide. Account deletion still cascades through
`user_id`.

`ix_push_delivery_subscription_id` exists only for that SET NULL — the unique
index leads on `notification_key`, so without it every retirement and every
`DELETE /me/push/subscriptions` would scan the log. Partial, because the rows it
has already nulled are never looked up by it again. `show_id` gets no such
index: shows are tombstoned rather than deleted (ADR-0005).

The two CHECK lists are written out here and built from tuples in
`app/models.py`; edit one, edit the other.

Revision ID: b7d3e5a91c40
Revises: a4e9c7d2b813
Create Date: 2026-09-26 23:30:00.000000+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b7d3e5a91c40"
down_revision: str | Sequence[str] | None = "a4e9c7d2b813"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "push_subscription",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("p256dh", sa.Text(), nullable=False),
        sa.Column("auth", sa.Text(), nullable=False),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_push_subscription"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["app.user.id"], ondelete="CASCADE", name="fk_push_subscription_user"
        ),
        sa.UniqueConstraint("endpoint", name="uq_push_subscription_endpoint"),
        schema="app",
    )
    op.create_index(
        "ix_push_subscription_user_id", "push_subscription", ["user_id"], schema="app"
    )

    op.create_table(
        "push_delivery",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("subscription_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("notification_key", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("show_id", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_push_delivery"),
        sa.ForeignKeyConstraint(
            ["subscription_id"],
            ["app.push_subscription.id"],
            ondelete="SET NULL",
            name="fk_push_delivery_subscription",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["app.user.id"], ondelete="CASCADE", name="fk_push_delivery_user"
        ),
        sa.ForeignKeyConstraint(
            ["show_id"], ["catalog.show.id"], ondelete="SET NULL", name="fk_push_delivery_show"
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["catalog.ingest_run.id"],
            ondelete="SET NULL",
            name="fk_push_delivery_run",
        ),
        sa.CheckConstraint(
            "kind IN ('airs_today', 'premiere_set', 'premiere_moved', 'ended', "
            "'revived', 'summary', 'test')",
            name="ck_push_delivery_kind",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'sent', 'failed', 'skipped')",
            name="ck_push_delivery_status",
        ),
        sa.UniqueConstraint(
            "notification_key", "subscription_id", name="uq_push_delivery_key_subscription"
        ),
        schema="app",
    )
    op.create_index(
        "ix_push_delivery_user_id_created_at",
        "push_delivery",
        ["user_id", "created_at"],
        schema="app",
    )
    op.create_index(
        "ix_push_delivery_subscription_id",
        "push_delivery",
        ["subscription_id"],
        schema="app",
        postgresql_where=sa.text("subscription_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_table("push_delivery", schema="app")
    op.drop_table("push_subscription", schema="app")
