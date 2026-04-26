"""add invite table

Revision ID: f8ceecec4809
Revises: e5867a694d27
Create Date: 2026-04-26 20:17:59.894191+00:00

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "f8ceecec4809"
down_revision = "e5867a694d27"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "invite",
        sa.Column("code", sa.Text(), primary_key=True),
        sa.Column("email_hint", postgresql.CITEXT(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["consumed_by_user_id"],
            ["app.user.id"],
            ondelete="SET NULL",
            name="fk_invite_consumed_by_user",
        ),
        schema="app",
    )


def downgrade() -> None:
    op.drop_table("invite", schema="app")
