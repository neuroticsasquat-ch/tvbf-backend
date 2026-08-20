"""Add app.connection_request_log — the connection-request ledger

NEU-1157. `POST /connection-requests` has no budget of any kind, and once
signup stops requiring an invite code (NEU-1156) that is the primary harassment
vector: one account can walk display names through `/users/search` and send
requests to everybody it finds.

The throttle cannot be built on `app.connection`, which is **deleted** on both
decline and cancel through one code path that cannot tell the two apart from
the row alone. Once a request resolves the database retains no evidence it
existed, so a cap counting `connection` rows is reset by cancelling — and the
two outcomes the reputation rule most needs are exactly the ones erased.

Retaining terminal states on `app.connection` instead was tried first and
rejected: it collides with `uq_connection_unordered_pair`, so a retained
`declined` row would 409 that pair forever, silently converting every decline
into a permanent block.

`outcome` is Text + a check constraint rather than a Postgres enum, following
`app.auth_attempt` and for its stated reason: widening a check constraint is a
visible, deliberately loud migration where `ALTER TYPE ... ADD VALUE` is a
one-liner that slips through review.

**No backfill.** The ledger starts empty. Existing accounts have no history, so
no adverse rate, so the full ceiling — which is what they would get under any
backfill option anyway, since the minimum sample is 5 and the whole userbase is
smaller than that. Backfilling `pending` rows would be actively worse than
nothing: it resurrects old requests as aging liabilities, so someone whose
friend never answered a request from May could be demoted by a migration. The
cost is that for the first 14 days after deploy there is no "ignored" signal for
anybody; declines and blocks work immediately.

Pruning is out of scope (spec §2.5) — this table is moderation evidence, and a
griefer's history auto-erasing on a 30-day timer is the wrong property for the
record an admin consults.

Revision ID: 0c8dc6199ab9
Revises: c4f81a6b2e57
Create Date: 2026-08-20 15:28:47.747394+00:00

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0c8dc6199ab9"
down_revision = "c4f81a6b2e57"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "connection_request_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("requester_id", UUID(as_uuid=True), nullable=False),
        sa.Column("addressee_id", UUID(as_uuid=True), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "outcome IN ('pending', 'accepted', 'declined', 'cancelled', 'blocked')",
            name="ck_connection_request_log_outcome",
        ),
        sa.ForeignKeyConstraint(
            ["requester_id"],
            ["app.user.id"],
            name="fk_connection_request_log_requester",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["addressee_id"],
            ["app.user.id"],
            name="fk_connection_request_log_addressee",
            ondelete="CASCADE",
        ),
        schema="app",
    )
    # Both *counting* queries are "one requester's rows since a timestamp". The
    # decline-cooldown lookup is not that shape — it filters the ordered pair on
    # `resolved_at`, which this index does not carry — but it leads on
    # `requester_id` too, and it is a `LIMIT 1` over a handful of rows.
    op.create_index(
        "ix_connection_request_log_requester_created",
        "connection_request_log",
        ["requester_id", "created_at"],
        schema="app",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_connection_request_log_requester_created",
        table_name="connection_request_log",
        schema="app",
    )
    op.drop_table("connection_request_log", schema="app")
