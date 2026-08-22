"""Record the raw title a recommendation was recovered from

NEU-1173. The model intermittently writes a comparison into `title` instead of
the bare series name — `"The Leftovers' 'Manhunt: Unabomber'"` — and the weekly
pass now reads the recommendation back out of the quotes when the title as
written resolves to nothing. `recovered_from` holds the **raw dressed title** on
such a row and NULL on every ordinary one.

The raw form rather than the extracted candidate: the candidate is re-derivable
from it, and the raw form is what is otherwise lost. `raw_response` preserves
every dressed title regardless; what this column adds is the join from a *stored
row* back to the title that produced it, so "did the fallback recover the right
show?" is answerable by query a month later, when the container logs have
rotated.

Nullable and unconstrained, so `create_all` in the test suite and Alembic here
have nothing to keep in sync beyond the column itself.

Revision ID: a7c31d5e8f04
Revises: d4b2e9c60a17
Create Date: 2026-08-17 18:10:00.000000+00:00

"""

import sqlalchemy as sa
from alembic import op

revision = "a7c31d5e8f04"
down_revision = "d4b2e9c60a17"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_recommendation",
        sa.Column("recovered_from", sa.Text(), nullable=True),
        schema="app",
    )


def downgrade() -> None:
    op.drop_column("user_recommendation", "recovered_from", schema="app")
