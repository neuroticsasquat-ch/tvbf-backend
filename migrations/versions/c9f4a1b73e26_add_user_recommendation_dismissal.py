"""Add app.user_recommendation_dismissal

NEU-1178. A user can remove one show from their recommendations, and no future
weekly pass may name it again. The row is the fifth source of the
never-recommend set (`recommendations/exclusion.py`) and is deliberately **not**
a taste signal: it never reaches `taste_for_user` and never lands in
`not_liked`, because a dismissal is a statement about one row rather than
something to generalise from.

Composite primary key on `(user_id, show_id)`, matching `user_show_watch`: the
pair is the fact, and the key doubles as `ON CONFLICT DO NOTHING`'s index
target. No standalone index on `show_id` for the same reason that table has
none — nothing filters on it alone, and shows are tombstoned rather than deleted
(ADR-0005), so the FK cascade check is not a hot path.

Every constraint is named here and in `app/models.py` alike, because the test
suite builds this table with `create_all` and production builds it from this
migration, and the two only agree when the names are stated.

Revision ID: c9f4a1b73e26
Revises: a7c31d5e8f04
Create Date: 2026-08-17 23:40:00.000000+00:00

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "c9f4a1b73e26"
down_revision = "a7c31d5e8f04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_recommendation_dismissal",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("show_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("user_id", "show_id", name="pk_user_recommendation_dismissal"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["app.user.id"], ondelete="CASCADE", name="fk_urd_user"
        ),
        sa.ForeignKeyConstraint(
            ["show_id"], ["catalog.show.id"], ondelete="CASCADE", name="fk_urd_show"
        ),
        schema="app",
    )


def downgrade() -> None:
    op.drop_table("user_recommendation_dismissal", schema="app")
