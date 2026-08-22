"""add issued_by_user_id to invite

Revision ID: 434b11ea2bda
Revises: f8ceecec4809
Create Date: 2026-08-21 12:00:00.000000+00:00

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "434b11ea2bda"
down_revision = "b4f8d2c7e619"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "invite",
        sa.Column("issued_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        schema="app",
    )
    op.create_foreign_key(
        "fk_invite_issued_by_user",
        source_table="invite",
        referent_table="user",
        local_cols=["issued_by_user_id"],
        remote_cols=["id"],
        source_schema="app",
        referent_schema="app",
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_invite_issued_by_user", table_name="invite", schema="app")
    op.drop_column("invite", "issued_by_user_id", schema="app")
