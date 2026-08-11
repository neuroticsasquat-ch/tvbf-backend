"""allow catalog.show.match_method = 'human' (NEU-1044)

The human matching queue resolves the mapping residue by hand, and its verdict
has to land in the database. `ck_show_match_method` (NEU-1043) permits only the
three tiers the automated pass can write, so `'human'` violates it.

Recording the verdict is the point. Setting `tmdb_id` and leaving `match_method`
NULL would make a reviewed mapping indistinguishable from a row nothing ever
matched — the precise ambiguity the column was added to remove — and under
ADR-0008 the row would additionally read as locally-authored, which it is not.

`'human'` with `tmdb_id IS NULL` is the other half of the same verdict: a
reviewer looked and found no TMDB counterpart, so the row stays locally-authored
deliberately rather than by omission. That is what lets the queue empty.

Revision ID: c1f7a5d20b93
Revises: 76da9dc2c5ed
Create Date: 2026-08-11 12:00:00.000000+00:00

"""

from collections.abc import Sequence

from alembic import op

revision: str = "c1f7a5d20b93"
down_revision: str | Sequence[str] | None = "76da9dc2c5ed"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = "match_method IS NULL OR match_method IN ('tvdb_id', 'imdb_id', 'title_year')"
_NEW = "match_method IS NULL OR match_method IN ('tvdb_id', 'imdb_id', 'title_year', 'human')"


def upgrade() -> None:
    op.drop_constraint("ck_show_match_method", "show", schema="catalog", type_="check")
    op.create_check_constraint("ck_show_match_method", "show", _NEW, schema="catalog")


def downgrade() -> None:
    # Not NOT VALID: the downgrade has to fail loudly if a human verdict exists,
    # rather than leave rows the constraint forbids sitting behind a constraint
    # that never checked them.
    op.drop_constraint("ck_show_match_method", "show", schema="catalog", type_="check")
    op.create_check_constraint("ck_show_match_method", "show", _OLD, schema="catalog")
