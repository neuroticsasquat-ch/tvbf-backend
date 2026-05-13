"""add app.auth_token table

Revision ID: 3a7c5e9b4f10
Revises: 9b3a7d2e4c10
Create Date: 2026-05-13 00:00:00.000000+00:00

"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "3a7c5e9b4f10"
down_revision = "9b3a7d2e4c10"
branch_labels = None
depends_on = None


auth_token_purpose = postgresql.ENUM(
    "email_verification",
    "password_reset",
    name="auth_token_purpose",
    schema="app",
)


def upgrade() -> None:
    auth_token_purpose.create(op.get_bind(), checkfirst=False)

    op.create_table(
        "auth_token",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column(
            "purpose",
            postgresql.ENUM(
                name="auth_token_purpose",
                schema="app",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app.user.id"],
            ondelete="CASCADE",
            name="fk_auth_token_user",
        ),
        schema="app",
    )

    op.create_index(
        "ix_auth_token_token_hash",
        "auth_token",
        ["token_hash"],
        schema="app",
    )
    op.create_index(
        "ix_auth_token_user_purpose_created",
        "auth_token",
        ["user_id", "purpose", "created_at"],
        schema="app",
    )


def downgrade() -> None:
    op.drop_index("ix_auth_token_user_purpose_created", table_name="auth_token", schema="app")
    op.drop_index("ix_auth_token_token_hash", table_name="auth_token", schema="app")
    op.drop_table("auth_token", schema="app")
    auth_token_purpose.drop(op.get_bind(), checkfirst=False)
