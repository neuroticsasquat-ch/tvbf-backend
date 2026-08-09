"""The one definition of TVBF's folded text form, and it is evaluated in Postgres.

Folding strips punctuation and whitespace (preserving letters of every script),
lowercases, and removes diacritics via the `unaccent` extension — so "shogun"
matches "Shōgun" and "spiderman" matches "Spider-Man", while non-Latin scripts
pass through unchanged and native-title search keeps working.

**It lives here so there is exactly one of it.** The fold is not reproducible in
Python: `unicodedata` normalisation does not decompose ł, ø, đ or ħ, so a
Python-side `unaccent` silently disagrees with the SQL one on precisely the
titles the fold exists for. Browse search has folded in SQL since it was written
(the `ix_*_folded_trgm` expression indexes are built on this expression); the
TMDB migration's title matching (NEU-1043) compares titles that arrive as JSON in
Python, and passes them in as **bind parameters** to this same expression rather
than growing a second, divergent definition.

If you are about to write `unicodedata.normalize(...)` to compare two titles,
use `folded_equal` instead.
"""

from sqlalchemy import Text, and_, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession


def folded(expr):
    """Accent- and punctuation-folded form of a text SQL expression or value.

    Takes a column, a literal, or a bind parameter — anything Postgres will
    accept as text — so the searched column and the query token can be folded by
    the identical expression.
    """
    stripped = func.regexp_replace(expr, "[[:punct:][:space:]]+", "", "g")
    return func.immutable_unaccent(func.lower(stripped))


async def folded_equal(session: AsyncSession, left: str, right: str) -> bool:
    """Whether two Python-side strings are equal once folded, decided by Postgres.

    One round trip, both sides bound as parameters into `folded`. That is what
    makes "exact title match" mean the same thing on both sides of the
    comparison — see the module docstring.

    **A title that folds to nothing never matches.** "!!!" and "???" both fold to
    the empty string, and treating those as the same title would be the one way
    this predicate could report an exact match between two unrelated shows.
    """
    left_folded = folded(literal(left, Text))
    stmt = select(and_(left_folded == folded(literal(right, Text)), left_folded != ""))
    return bool((await session.execute(stmt)).scalar_one())
