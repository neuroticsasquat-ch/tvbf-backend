"""Resolution: a model-authored title and year to a `catalog.show` surrogate id.

Project spec §8. The model answers with prose — a title somebody typed into a
training corpus — and everything downstream of it needs a row id. This module is
the whole of that translation, and it is **entirely local**: no upstream call, so
ADR-0002 holds without exception and resolution costs nothing per recommendation.

Two tiers, in order, first hit wins:

1. fold-exact on `catalog.show.name`, premiere year within ±1
2. fold-exact on `catalog.show_aka.title`, premiere year within ±1

and nothing after that — an unresolved title is `None`, which the caller drops
and logs.

## The AKA tier earns its place

A model recommending international television names shows in English, and
`catalog.show_aka` holds TMDB's `alternative_titles`. That is the step that
catches "Money Heist" for *La casa de papel*, and it is why a second query is
worth its round trip.

## No fuzzy or trigram fallback

A similarity threshold solves a *human search box* problem — typos — that does
not exist here. An LLM naming television series either knows the show or invented
one, so a threshold buys almost no true matches while converting every
hallucination into a confident wrong match, and it introduces a magic number
nobody can calibrate. **A resolution failure is useful signal, not a defect to
paper over**: an unmatched title is either a hallucination or a genuine catalog
gap, and both belong in the logs rather than silently mapped onto whatever scored
0.7. The two trigram indexes this module's equality reads use
(`ix_show_name_folded_trgm`, `ix_show_aka_title_folded_trgm`, migration
`aa4571de8f17`) are not an invitation to start reading them with `%`.

## Ambiguity resolves by `popularity`, not to unmatched

Reboots and remakes fold equal at the same year. Take the higher `popularity`.
This deliberately **diverges from NEU-1043**, where every ambiguity resolves to
unmatched: there a false positive silently attached a user's watch history to the
wrong show and was unrecoverable, here it shows a less-likely card in a grid of
twelve. The costs are not comparable and the rules should not be either.

## The fold is `sql_fold`'s, and the year is the only other input

The model-authored title arrives as JSON in Python and is bound as a **parameter**
into the same expression the indexed columns are folded by — the same thing
NEU-1043's title matching does, and for the same reason: there is exactly one
fold, it is evaluated in Postgres, and it is not reproducible in Python
(`unicodedata` does not decompose ł, ø, đ, ħ).

`year` is required rather than optional because it is the only disambiguator
resolution has; without it tier 1 is unbounded (project spec §7, which is also
why a recommendation arriving without a `release_year` is dropped by the caller
rather than resolved with a wildcard here). A show with no `first_air_date` never
matches — there is no year to compare, not a year to guess.

## What this module deliberately does not decide

**The exclusions are the caller's.** Never recommending a show the user already
has a record for is enforced against the taste payload, which is that same set
(§8) — a resolver that also took a user id would be answering two questions and
could not be reused by anything that is not the weekly pass.

**`adult` and `deleted_upstream_at` are filtered at read time** (§8), which is
what the 25-requested / 12-displayed headroom is for: a set generated in March can
contain a show tombstoned in June, so the read path has to filter regardless and a
write-time filter here would only be a second, weaker copy of it — one that also
makes a resurrected show permanently unrecommendable.
"""

from dataclasses import dataclass

from sqlalchemy import Text, extract, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.models import MATCHED_VIA_AKA, MATCHED_VIA_NAME
from tvbf.catalog.models import Show, ShowAka
from tvbf.sql_fold import folded

# How far a model-authored year may sit from the mirrored premiere year and still
# be the same show. One either side, because the two catalogues disagree about
# which side of a New Year a December premiere falls on, and because a model
# reporting the year a show "came out" may mean its US premiere rather than its
# original one.
YEAR_TOLERANCE = 1


@dataclass(frozen=True, slots=True)
class Resolution:
    """A resolved recommendation: which show, and which tier said so.

    `matched_via` is one of `app.models.RECOMMENDATION_MATCHED_VIA` and is stored
    on the row for the reason NEU-1043's `match_method` exists: it makes one tier
    retractable as a batch — a `WHERE` clause rather than a re-run of every user.
    """

    show_id: int
    matched_via: str


async def resolve(db: AsyncSession, *, title: str, year: int) -> Resolution | None:
    """The show this title and year name, or `None` if nothing does.

    `None` is a real answer and the caller's job is to drop the recommendation and
    log the raw title — see the module docstring on why there is no third tier.
    """
    folded_title = folded(literal(title, Text))
    # A title that folds to nothing never matches. "!!!" and "???" both fold to
    # the empty string, so without this a model naming one punctuation-only show
    # resolves to whichever unrelated punctuation-only show is more popular — the
    # one way an *exact* match could be wrong. `sql_fold.folded_equal` guards its
    # own comparison the same way.
    matchable = folded_title != ""
    premiered_near = _premiered_near(year)

    by_name = _most_popular(folded(Show.name) == folded_title, matchable, premiered_near)
    show_id = (await db.execute(by_name)).scalars().first()
    if show_id is not None:
        return Resolution(show_id=show_id, matched_via=MATCHED_VIA_NAME)

    # Joined only here. A show carrying several AKAs that fold equal yields
    # several rows, which `limit(1)` collapses — they all name the same show.
    by_aka = _most_popular(folded(ShowAka.title) == folded_title, matchable, premiered_near).join(
        ShowAka, ShowAka.show_id == Show.id
    )
    show_id = (await db.execute(by_aka)).scalars().first()
    if show_id is not None:
        return Resolution(show_id=show_id, matched_via=MATCHED_VIA_AKA)

    return None


def _most_popular(*criteria):
    """The single best `catalog.show` id satisfying `criteria`.

    Highest `popularity` wins an ambiguity, and NULLs lose: Postgres sorts them
    first on DESC, which would otherwise hand every tie to a show TMDB has no
    popularity for. `Show.id` breaks the remaining tie, so the answer is one row
    rather than whichever row the planner happened to emit first — a resolver
    that returns a different show run to run is one nobody can debug from the
    stored set afterwards.
    """
    return (
        select(Show.id)
        .where(*criteria)
        .order_by(Show.popularity.desc().nulls_last(), Show.id)
        .limit(1)
    )


def _premiered_near(year: int):
    """`catalog.show.first_air_date` falls within ±`YEAR_TOLERANCE` of `year`.

    An undated show is excluded rather than admitted: `extract` of NULL is NULL,
    which `BETWEEN` answers falsely, and that is the behaviour wanted — the year
    is the only disambiguator there is, so a row that cannot be checked against it
    has not been checked at all.
    """
    return extract("year", Show.first_air_date).between(
        year - YEAR_TOLERANCE, year + YEAR_TOLERANCE
    )
