"""'add immutable_unaccent and folded trgm index'

Revision ID: c2e451aa1ec6
Revises: fe2ae8ac172b
Create Date: 2026-06-29 17:12:21.612392+00:00

"""
from alembic import op


revision = 'c2e451aa1ec6'
down_revision = 'fe2ae8ac172b'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE OR REPLACE FUNCTION public.immutable_unaccent(text)
        RETURNS text
        LANGUAGE sql
        IMMUTABLE STRICT
        AS $$ SELECT public.unaccent($1) $$
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_show_name_folded_trgm
        ON tvmaze.show
        USING gin (
            immutable_unaccent(lower(regexp_replace(name, '[[:punct:][:space:]]+', '', 'g')))
            gin_trgm_ops
        )
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS tvmaze.ix_show_name_folded_trgm")
    op.execute("DROP FUNCTION IF EXISTS public.immutable_unaccent(text)")
