"""'add folded trgm index on person name'

Revision ID: b3f7c1d9a204
Revises: 57e72de0a940
Create Date: 2026-08-02 16:10:00.000000+00:00

"""
from alembic import op


revision = 'b3f7c1d9a204'
down_revision = '57e72de0a940'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Same shape as ix_show_name_folded_trgm (c2e451aa1ec6): person search folds
    # both the column and the query token, so the leading-wildcard LIKE needs a
    # GIN trigram index over the folded expression. ~487k rows is well past the
    # point where a seq scan per keystroke is felt.
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_person_name_folded_trgm
        ON tvmaze.person
        USING gin (
            immutable_unaccent(lower(regexp_replace(name, '[[:punct:][:space:]]+', '', 'g')))
            gin_trgm_ops
        )
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS tvmaze.ix_person_name_folded_trgm")
