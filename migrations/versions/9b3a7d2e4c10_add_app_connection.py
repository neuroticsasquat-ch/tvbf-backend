"""add app.connection table

Revision ID: 9b3a7d2e4c10
Revises: 2c05c4a1d7cb
Create Date: 2026-05-08 00:00:00.000000+00:00

"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "9b3a7d2e4c10"
down_revision = "2c05c4a1d7cb"
branch_labels = None
depends_on = None


connection_state = postgresql.ENUM(
    "pending",
    "accepted",
    "blocked",
    name="connection_state",
    schema="app",
)


def upgrade() -> None:
    connection_state.create(op.get_bind(), checkfirst=False)

    op.create_table(
        "connection",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("requester_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("addressee_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "state",
            postgresql.ENUM(
                name="connection_state",
                schema="app",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["requester_id"],
            ["app.user.id"],
            ondelete="CASCADE",
            name="fk_connection_requester",
        ),
        sa.ForeignKeyConstraint(
            ["addressee_id"],
            ["app.user.id"],
            ondelete="CASCADE",
            name="fk_connection_addressee",
        ),
        sa.CheckConstraint(
            "requester_id <> addressee_id",
            name="ck_connection_not_self",
        ),
        schema="app",
    )

    op.create_index(
        "uq_connection_unordered_pair",
        "connection",
        [
            sa.func.least(sa.column("requester_id"), sa.column("addressee_id")),
            sa.func.greatest(sa.column("requester_id"), sa.column("addressee_id")),
        ],
        unique=True,
        schema="app",
    )
    op.create_index(
        "ix_connection_requester_state",
        "connection",
        ["requester_id", "state"],
        schema="app",
    )
    op.create_index(
        "ix_connection_addressee_state",
        "connection",
        ["addressee_id", "state"],
        schema="app",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_connection_addressee_state", table_name="connection", schema="app"
    )
    op.drop_index(
        "ix_connection_requester_state", table_name="connection", schema="app"
    )
    op.drop_index(
        "uq_connection_unordered_pair", table_name="connection", schema="app"
    )
    op.drop_table("connection", schema="app")
    connection_state.drop(op.get_bind(), checkfirst=False)
