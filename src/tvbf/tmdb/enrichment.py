"""Attach `tmdb_id` to the copied catalog rows, in three tiers (NEU-1043).

NEU-1042 put every TV Maze show into `catalog` with its id preserved and
`tmdb_id IS NULL`. This is the pass that fills that null in, and it is the last
place the two catalogues are ever reconciled.

**Transitional scaffolding, and that is a design constraint rather than a
caveat.** There is no scheduled job here, no monthly re-sweep and no delta —
map once, resolve the residue through NEU-1044's human queue, verify, cut over.
It runs *after* `tvbf.jobs.catalog_copy` and *before* the full TMDB ingest
(NEU-1034); the copy's docstring explains why that order is the one that needs
no special handling anywhere.

## The three tiers

| Tier | Method | `match_method` | Exact? |
| -- | -- | -- | -- |
| 1 | `/find` by `tvdb_id` | `tvdb_id` | yes |
| 2 | `/find` by `imdb_id` | `imdb_id` | yes |
| 3 | `/search/tv` by title + year | `title_year` | **no** |

Tiers run in order per show and stop at the first hit, so tier 2 is reached only
when a show has no `tvdb_id` or TMDB does not know the one it has.

**An ambiguous exact lookup stops the show outright** — it does not fall through
to the next tier. Two TMDB series claiming one `tvdb_id` is the strongest signal
available saying *we do not know which of these you are*; answering it with a
title guess would resolve the clearest ambiguity with the weakest tier.

**Tier 3 accepts a match only when all three of these hold**, and there is no
scoring, no threshold and no best-guess anywhere in it:

1. the search returned **exactly one** result overall (`total_results == 1`);
2. that result's title equals ours **in the folded form** (see below); and
3. its `first_air_date` year is within ±1 of our premiere year.

Shows with **no premiere date are excluded from tier 3 entirely** — a title with
no year behind it is not enough signal, and unmatched title collisions cluster in
exactly that population. They fall to the human queue, which is the right place
for them.

The conservative reading of (1) is deliberate. A false positive silently attaches
a user's watch history to the wrong show and nothing downstream would catch it; a
false negative surfaces as one more row in a queue that is already being worked
by hand. Those costs are not comparable, so ambiguity always resolves to
unmatched.

## Both sides of a title comparison fold through Postgres

`tvbf.sql_fold` holds the one definition, and `folded_equal` binds both titles
into it. A Python-side `unaccent` diverges from the SQL one on ł/ø/đ/ħ — the
characters the fold exists for — so "exact title match" would otherwise mean two
different things on the two sides of the comparison.

## Idempotence

Candidates are `tmdb_id IS NULL`, and the UPDATE re-asserts that, so **a re-run
cannot change an existing match** — exact or otherwise. What a re-run *does* redo
is every show that failed, which is deliberate: the cheap way to pick up a show
TMDB has since added is to run the pass again.

A show whose `tmdb_id` is already held by another catalog row is left unmatched
and counted as a collision rather than raising. TV Maze carries genuine duplicate
show entries, and two of them mapping to one TMDB series is data to look at in
the human queue, not a reason to kill a three-and-a-half-hour pass.

## Known limitation: tiers are ordered within a show, not between shows

**This module has a real bug and it was deliberately not fixed.** Candidates
stream in `ORDER BY id` and the first row to claim a `tmdb_id` keeps it, so a
tier-3 title guess on a low-id row can beat a tier-1 exact `/find` hit on a
higher-id row — resolving the clearest evidence of ambiguity with the weakest
tier. The production run hit it 18 times out of 107 collisions (NEU-1065); the
worked case is `Lads Army` taking TMDB 747 from `Bad Lads Army`, which held the
exact tvdb link.

Those 18 rows were repaired by hand — `docs/migration/neu-1043-collision-remediation.sql`.
The fix (two sweeps: every exact tier across the whole catalog first, then
title+year over the remainder) was **superseded rather than implemented**,
because the window in which it could matter has closed:

- a re-run skips rows that already have a `tmdb_id`, so the 8,631 existing
  `title_year` matches would never be re-evaluated, and the unmatched remainder
  has already failed all three tiers; and
- **after the full TMDB ingest, every series has a catalog row carrying its
  `tmdb_id`**, so anything this module matches is refused by the `NOT EXISTS`
  guard and logged as a collision. It cannot match at all past that point.

If you ever re-map from scratch — the one scenario that revives the problem —
implement the two-sweep order first. NEU-1065 has the design.
"""

import logging
from collections import Counter
from dataclasses import dataclass
from datetime import date

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.sql_fold import folded_equal
from tvbf.tmdb.client import TMDBClient

log = logging.getLogger(__name__)

# The `match_method` vocabulary, mirrored by `ck_show_match_method`.
MATCH_TVDB_ID = "tvdb_id"
MATCH_IMDB_ID = "imdb_id"
MATCH_TITLE_YEAR = "title_year"
MATCH_METHODS: tuple[str, ...] = (MATCH_TVDB_ID, MATCH_IMDB_ID, MATCH_TITLE_YEAR)

# Outcomes that are not a match. Local to the tally; never written to a row.
_UNMATCHED = "unmatched"
_COLLISION = "collision"

# How far a TMDB `first_air_date` year may sit from our premiere year in tier 3.
#
# One, not zero: the two sources disagree about what a premiere is often enough
# to matter — a festival or preview airing against a broadcast debut, and a
# December premiere that TMDB dates to the following January. Two would start
# admitting the reboot of a show whose original is the row being matched.
_PREMIERE_YEAR_DRIFT = 1

# Shows per commit. The pass is ~3.5 hours over 89k shows, so it has to
# checkpoint: a crash three hours in must not throw away three hours of
# upstream calls. Small enough that a batch is seconds of work, large enough
# that commits are not the cost.
_BATCH_SIZE = 500


@dataclass(frozen=True)
class ShowToMatch:
    """The columns the tiers read. Not the ORM row — this is all of it."""

    id: int
    name: str
    tvdb_id: int | None
    imdb_id: str | None
    first_air_date: date | None


@dataclass(frozen=True)
class EnrichmentResult:
    considered: int
    # Keyed by `match_method`; every method present, zeroes included, so a tier
    # that stopped matching reads as 0 rather than going missing.
    by_method: dict[str, int]
    unmatched: int
    collisions: int

    @property
    def matched(self) -> int:
        return sum(self.by_method.values())


# Keyset paging rather than OFFSET: a matched row leaves the candidate set the
# moment it is written, so an offset would step over the row that slid into its
# place. Ordering by id makes `id > :after_id` exactly "everything not yet seen".
_CANDIDATES = text("""
    SELECT id, name, tvdb_id, imdb_id, first_air_date
    FROM catalog.show
    WHERE tmdb_id IS NULL AND id > :after_id
    ORDER BY id
    LIMIT :limit
""")

_REMAINING = text("SELECT count(*) FROM catalog.show WHERE tmdb_id IS NULL")

# Two guards, both load-bearing.
#
# `tmdb_id IS NULL` is what makes "re-running does not change existing exact
# matches" a property of the statement rather than of the query that fed it.
#
# `NOT EXISTS` is the collision check. `uq_show_tmdb_id` would otherwise raise
# and take the surrounding batch's transaction with it, losing work that had
# nothing to do with the duplicate. Checking here turns it into a counted
# outcome. It covers this row too, since this row's `tmdb_id` is null.
_ATTACH = text("""
    UPDATE catalog.show
    SET tmdb_id = :tmdb_id, match_method = :match_method
    WHERE id = :id
      AND tmdb_id IS NULL
      AND NOT EXISTS (SELECT 1 FROM catalog.show other WHERE other.tmdb_id = :tmdb_id)
""")


def _year(air_date: str | None) -> int | None:
    """The year in a TMDB `first_air_date`, which is `"YYYY-MM-DD"`, `""` or absent."""
    if not air_date:
        return None
    try:
        return int(air_date[:4])
    except ValueError:
        log.warning("unparseable TMDB first_air_date %r", air_date)
        return None


async def _find_series(client: TMDBClient, external_id: str, external_source: str) -> list[int]:
    """Every TMDB series id `/find` answers with for one exact upstream id.

    Returns the whole list rather than "the one, if there is exactly one",
    because **none and several are different answers** and only the caller can
    act on the difference — see `_match`.
    """
    payload = await client.find_by_external_id(external_id, external_source)
    results = payload.get("tv_results") or []
    return [r["id"] for r in results if r.get("id") is not None]


async def _match_by_title_year(
    session: AsyncSession, client: TMDBClient, show: ShowToMatch
) -> int | None:
    """Tier 3. Returns a series id only when all three conditions hold."""
    if show.first_air_date is None:
        # Excluded from the tier entirely — no request is spent on it.
        return None
    if not show.name.strip():
        return None

    payload = await client.search_tv(show.name)
    # `total_results` and the page length are both checked: the first is the
    # condition the rule states, the second is what this code actually indexes
    # into, and they can disagree if TMDB ever changes its paging.
    if payload.get("total_results") != 1:
        return None
    results = payload.get("results") or []
    if len(results) != 1:
        return None

    result = results[0]
    tmdb_id = result.get("id")
    if tmdb_id is None:
        return None

    year = _year(result.get("first_air_date"))
    if year is None or abs(year - show.first_air_date.year) > _PREMIERE_YEAR_DRIFT:
        return None

    # Last, because it is the only condition that costs a round trip. TMDB's
    # `name` and not `original_name`: our title came from TV Maze, which
    # catalogues in the same English-or-romanised register TMDB's default
    # `name` does, so comparing against the native-script original would reject
    # true matches rather than admit them.
    if not await folded_equal(session, show.name, result.get("name") or ""):
        return None
    return tmdb_id


async def _match(
    session: AsyncSession, client: TMDBClient, show: ShowToMatch
) -> tuple[str, int] | None:
    """The tiers, in order, stopping at the first hit.

    **An ambiguous exact lookup ends the show, rather than falling through.**
    Two TMDB series claiming one `tvdb_id` is upstream telling us it does not
    know which of them this row is — the strongest signal available, and it says
    *contested*. Treating that as a miss would let the answer come from a title
    guess instead, i.e. resolve the strongest evidence of ambiguity with the
    weakest tier. That is exactly the best-guess the mapping rule forbids, so it
    stops here and goes to the human queue.
    """
    # (match_method, TMDB external_source, our value). The first two strings
    # coincide today — TMDB happens to name its sources the way these tiers are
    # named — but they are two vocabularies and only one of them is ours, so
    # they are written out rather than shared.
    exact_tiers = (
        (MATCH_TVDB_ID, "tvdb_id", None if show.tvdb_id is None else str(show.tvdb_id)),
        (MATCH_IMDB_ID, "imdb_id", show.imdb_id or None),
    )
    for method, source, external_id in exact_tiers:
        if external_id is None:
            continue
        found = await _find_series(client, external_id, source)
        if len(found) > 1:
            log.warning(
                "catalog show %d: /find %s=%s returned %d series — upstream disagrees "
                "about which one this is, so no tier may match it",
                show.id,
                source,
                external_id,
                len(found),
            )
            return None
        if found:
            return method, found[0]

    by_title = await _match_by_title_year(session, client, show)
    if by_title is not None:
        return MATCH_TITLE_YEAR, by_title
    return None


async def _match_and_attach(session: AsyncSession, client: TMDBClient, show: ShowToMatch) -> str:
    """Match one show and write the result. Returns the tally key for it."""
    match = await _match(session, client, show)
    if match is None:
        return _UNMATCHED

    method, tmdb_id = match
    result = await session.execute(
        _ATTACH, {"id": show.id, "tmdb_id": tmdb_id, "match_method": method}
    )
    if result.rowcount == 0:  # type: ignore[attr-defined]
        log.warning(
            "catalog show %d matched TMDB %d via %s, but another catalog row already "
            "holds it — left unmatched for the human queue",
            show.id,
            tmdb_id,
            method,
        )
        return _COLLISION
    return method


async def enrich_show_ids(
    session: AsyncSession,
    client: TMDBClient,
    *,
    limit: int | None = None,
    batch_size: int = _BATCH_SIZE,
) -> EnrichmentResult:
    """Map every unmapped `catalog.show` onto a `tmdb_id`, and report what landed.

    **This function owns its transaction boundaries**, unlike `copy_to_catalog`
    next door. The copy is 44 seconds of SQL that either happened or did not;
    this is three and a half hours of upstream calls, and work that cannot be
    reproduced for free has to be committed as it is done.

    `limit` caps how many shows are considered — the way to try a hundred before
    committing to the full pass against a paced API.
    """
    outcomes: Counter[str] = Counter()
    total = (await session.execute(_REMAINING)).scalar_one()
    log.info(
        "enrichment: %d shows have no tmdb_id%s", total, f", considering {limit}" if limit else ""
    )

    after_id = 0
    considered = 0
    while limit is None or considered < limit:
        take = batch_size if limit is None else min(batch_size, limit - considered)
        rows = (await session.execute(_CANDIDATES, {"after_id": after_id, "limit": take})).all()
        if not rows:
            break

        for row in rows:
            show = ShowToMatch(
                id=row.id,
                name=row.name,
                tvdb_id=row.tvdb_id,
                imdb_id=row.imdb_id,
                first_air_date=row.first_air_date,
            )
            after_id = show.id
            considered += 1
            outcomes[await _match_and_attach(session, client, show)] += 1

        await session.commit()
        log.info(
            "enrichment: %d/%d considered — %d by tvdb_id, %d by imdb_id, %d by title+year, "
            "%d unmatched, %d collisions",
            considered,
            total,
            outcomes[MATCH_TVDB_ID],
            outcomes[MATCH_IMDB_ID],
            outcomes[MATCH_TITLE_YEAR],
            outcomes[_UNMATCHED],
            outcomes[_COLLISION],
        )

    return EnrichmentResult(
        considered=considered,
        by_method={method: outcomes[method] for method in MATCH_METHODS},
        unmatched=outcomes[_UNMATCHED],
        collisions=outcomes[_COLLISION],
    )
