"""add catalog.show_recommendation and catalog.show.recommendations_synced_at (NEU-1052)

The "More like this" surface's storage, and the watermark the one-time backfill
that fills it resumes on.

`show_recommendation` is a show-to-show edge carrying TMDB's own position:
`(source_show_id, rank) -> target_show_id`, twenty rows per source show. There is
deliberately **no `source` column** — `/tv/{id}/recommendations` is the only
endpoint that writes here (`/similar` was measured and rejected, project spec
§2), so the column would hold one constant forever.

`recommendations_synced_at` is the third watermark on `catalog.show`, added for
the same reason `credits_synced_at` was the second: "the show carries no
`show_recommendation` row" cannot tell *upstream returned none* from *nobody has
asked*, and upstream returning none is ~8% of the zero-vote long tail. Under that
predicate those shows would be re-fetched on every run and the pass would never
converge.

Nullable with no backfill and no index. NULL is the correct value for every
existing row — the 229,418 already-mirrored shows were synced before the
namespace was appended — and the backfill's work list pages by an ordering key,
not by a scan of this column.

Revision ID: f3a71c9d24b8
Revises: b84de53c9f1d
Create Date: 2026-08-16 16:20:00.000000+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f3a71c9d24b8"
down_revision: str | Sequence[str] | None = "b84de53c9f1d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "show",
        sa.Column("recommendations_synced_at", sa.DateTime(timezone=True), nullable=True),
        schema="catalog",
    )
    op.create_table(
        "show_recommendation",
        sa.Column("source_show_id", sa.BigInteger(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("target_show_id", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["source_show_id"], ["catalog.show.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["target_show_id"], ["catalog.show.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("source_show_id", "rank", name="pk_show_recommendation"),
        sa.UniqueConstraint(
            "source_show_id", "target_show_id", name="uq_show_recommendation_target"
        ),
        schema="catalog",
    )
    # Not for reading — this is the CASCADE's index. Without it every show
    # deletion sequentially scans the whole table looking for referencing rows.
    op.create_index(
        "ix_show_recommendation_target_show_id",
        "show_recommendation",
        ["target_show_id"],
        unique=False,
        schema="catalog",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_show_recommendation_target_show_id", table_name="show_recommendation", schema="catalog"
    )
    op.drop_table("show_recommendation", schema="catalog")
    op.drop_column("show", "recommendations_synced_at", schema="catalog")
