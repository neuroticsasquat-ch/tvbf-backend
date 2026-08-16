"""Add the recommendation set and recommendation tables (NEU-1106)

`app.user_recommendation_set` is one generated batch for one user and
`app.user_recommendation` is one resolved suggestion inside it.

**The set row is what makes the weekly swap atomic and non-destructive**
(project spec §9): the pass inserts a new set with its rows and the previous set
stops being the newest, so nothing is deleted ahead of a write that might fail
and a provider outage leaves last week's recommendations standing rather than
blanking the section. It doubles as the run record — there is deliberately no
`ingest_run` row and no run table for this job, because a set already carries
status, timing, tokens and the raw response per user.

`compiled_payload` + `raw_response` are how a bad recommendation gets diagnosed:
what the model was told *and* what it said. That puts a second copy of the
user's watch history in the database, which is why `user_id` cascades — it is
what keeps account deletion complete, deliberately unlike `app.watch_archive`,
which carries no foreign key at all.

Both vocabularies (`status`, `matched_via`) are ours rather than upstream
values, so both are guarded by CHECK constraints on `ck_show_match_method`'s
precedent.

Revision ID: b84de53c9f1d
Revises: d5f2a91c6b03
Create Date: 2026-08-15 19:32:56.768915+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b84de53c9f1d"
down_revision: str | Sequence[str] | None = "d5f2a91c6b03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_recommendation_set",
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column(
            "generated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("payload_hash", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("compiled_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("raw_response", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.CheckConstraint(
            "status IN ('succeeded', 'failed', 'no_matches', 'insufficient_history')",
            name="ck_user_recommendation_set_status",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["app.user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        schema="app",
    )
    op.create_index(
        "ix_user_recommendation_set_user_generated",
        "user_recommendation_set",
        ["user_id", "generated_at"],
        unique=False,
        schema="app",
    )
    op.create_table(
        "user_recommendation",
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("set_id", sa.UUID(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("show_id", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("matched_via", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "matched_via IN ('name', 'aka')",
            name="ck_user_recommendation_matched_via",
        ),
        sa.ForeignKeyConstraint(
            ["set_id"], ["app.user_recommendation_set.id"], ondelete="CASCADE"
        ),
        # Named explicitly, like every other `app` -> `catalog` foreign key: the
        # test suite builds this table with `create_all` and production builds it
        # with Alembic, so the two have to agree on the constraint's name.
        sa.ForeignKeyConstraint(
            ["show_id"],
            ["catalog.show.id"],
            name="fk_user_recommendation_show",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("set_id", "rank", name="uq_user_recommendation_set_rank"),
        schema="app",
    )
    op.create_index(
        "ix_user_recommendation_show_id",
        "user_recommendation",
        ["show_id"],
        unique=False,
        schema="app",
    )


def downgrade() -> None:
    op.drop_index("ix_user_recommendation_show_id", table_name="user_recommendation", schema="app")
    op.drop_table("user_recommendation", schema="app")
    op.drop_index(
        "ix_user_recommendation_set_user_generated",
        table_name="user_recommendation_set",
        schema="app",
    )
    op.drop_table("user_recommendation_set", schema="app")
