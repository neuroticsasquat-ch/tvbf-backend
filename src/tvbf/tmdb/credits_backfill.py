"""Fill the credit tables for shows the ingest mirrored before the writers existed (NEU-1127).

## What happened

The full catalog ingest (NEU-1034) ran 2026-08-10 → 2026-08-11 and mirrored
228,723 series. `_write_credits` (NEU-1039) merged at 04:54 on 2026-08-11 and
`_write_episode_credits` (NEU-1040) at 05:24 — after it. `aggregate_credits` had
been in `DEFAULT_APPEND` since 2026-08-09 and the season blocks have always
carried `guest_stars` / `crew`, so **every payload the ingest fetched contained
the credits**; there was simply nothing to write them to. Production therefore
holds 228,841 fully-mirrored shows and five empty tables — `show_cast`,
`show_crew`, `episode_guest_cast`, `episode_crew`, `person` — behind read paths
NEU-1047 has already repointed onto them.

This is the third instance of one pattern, after NEU-1045 and NEU-1126: **merged
is not run**. The production run log in `docs/migration/README.md` is where that
gets recorded.

## Why re-running the ingest is not the fix

Its work list is `tmdb_synced_at IS NULL`, and all 228,841 rows are stamped —
the same shut window NEU-1045 hit. Clearing the stamp and re-running was
considered and rejected: identical cost, but it rewrites 228k shows and 6.5M
episodes to recover four tables that ride the same request, and every one of
those writes is a chance to disturb a spine users are now reading from. So this
pass fetches the same payload and writes **only** the credits, through
`upsert.write_series_credits` — which is the seam that makes "writes nothing
else" a property of the writer rather than a promise made by the caller.

The daily delta does now write credits, since `mirror_series` is shared, but it
only visits shows TMDB reports as changed (1,567 in the one run so far), so it
would never work through the backlog.

## The watermark is a column, and that was the decision to make

Two devices were available. **"The show carries no `show_cast` row"** needs no
migration and is self-clearing the way `enrichment.py`'s work list is. **A
watermark column** costs a migration. The column wins, for one reason:

*A show TMDB has no credits for is a normal outcome, not a failure* — and the
predicate cannot represent it. Under it, every credit-less series looks
identical to one nobody has fetched yet, so each run re-fetches all of them and
the pass never converges; "done" stops being observable. That is precisely the
conflation `catalog.show.tmdb_synced_at` exists to avoid one grain up, where
"already ingested" and "row already present" were also different questions and
the ingest resuming on row-existence would have skipped exactly the shows users
track.

So the work list is `tmdb_synced_at IS NOT NULL AND credits_synced_at IS NULL`,
`mark_credits_synced` stamps a show whether or not upstream had anything for it,
and `mark_series_synced` stamps both columns so a show the delta has already
covered never enters the backlog.

## Shape and cost

One request per show over ~228k shows, the full `DEFAULT_APPEND` plus the
speculative season window, exactly as the full pass fetches — **~8.7 hours** at
the measured 7.27 shows/sec, because the loop is sequential and round-trip
latency binds rather than the 20 req/s budget. Safe to kill and restart: a show
leaves the work list only once its credits are committed and stamped, in the
same transaction.

Per-show failures are counted and stepped over, `_FAILURE_THRESHOLD`
consecutive real ones raise, and a 404 is neither — a series TMDB has deleted
since the ingest is a data condition (NEU-1006), and the show stays unstamped.
"""

import logging
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.tmdb.client import TMDBClient, is_gone_upstream
from tvbf.tmdb.ingest import fetch_series_with_seasons
from tvbf.tmdb.upsert import mark_credits_synced, write_series_credits

log = logging.getLogger(__name__)

# How many shows one candidate query takes. Not a commit boundary — this pass
# commits per show, the way `mirror_series` does, because a show's credits are
# hundreds to thousands of rows and batching fifty of those into one transaction
# buys nothing but a longer lock hold against a spine the app is now serving
# reads from. At 7.3 shows/sec that is ~7 commits a second, which Postgres does
# not notice, and an interrupt costs at most one show's request.
_PAGE_SIZE = 200

# Consecutive per-show failures before the pass gives up, the threshold every
# other TMDB pass uses. A 404 does not count.
_FAILURE_THRESHOLD = 10

# How often to log a running total — a line every ~2 minutes at the measured
# rate, the same cadence `tmdb/ingest.py` picked for the same 8.7-hour window.
_PROGRESS_EVERY = 1000


class CreditsBackfillAborted(Exception):
    """Too many consecutive per-show failures. Ends the pass; the log has the rest."""


class MissingCreditsNamespace(Exception):
    """The payload came back without the `aggregate_credits` we asked for.

    Treated as a per-show failure rather than as "this show has no credits",
    because the request always appends the namespace — so its absence describes
    the response, not the series. Stamping on it would retire the show from the
    backlog having never seen its credits, which is the exact silent partial
    this whole ticket exists to repair one level up.

    The cost of being wrong in this direction is a show retried on every run and
    counted in the failure column, where the other direction is a show lost from
    the work list with nothing to say so.
    """


@dataclass(frozen=True)
class ShowToBackfill:
    """A mirrored show whose credits have not been written. Not the ORM row — this is all of it."""

    id: int
    tmdb_id: int
    name: str


@dataclass(frozen=True)
class CreditsBackfillResult:
    shows_considered: int
    shows_stamped: int
    shows_failed: int
    shows_gone: int
    # Stamped, but upstream carried no cast, no crew, no guest stars and no
    # episode crew. Counted apart from `shows_stamped` because it is the outcome
    # a reader would otherwise mistake for a failure — and because a *rising*
    # share of it is the one sign the payload has stopped carrying credits.
    shows_without_credits: int


# What makes a show worth a request: a complete payload has been mirrored onto it
# and no pass has written its credits. Written once and shared by the work list
# and its count — the denominator in the progress log means nothing if it drifts
# from what the loop actually takes.
#
# `tmdb_id IS NOT NULL` is implied by `tmdb_synced_at` and restated anyway: it is
# the id the request is made with, and a locally-authored row (ADR-0008) has none
# to make it with.
_NEEDS_CREDITS = """
    s.tmdb_id IS NOT NULL
    AND s.tmdb_synced_at IS NOT NULL
    AND s.credits_synced_at IS NULL
"""

# Keyset paging rather than OFFSET, as `enrichment.py` and `episode_map.py` do
# it: a show leaves the candidate set the moment it is stamped, so an offset
# would step over whatever slid into its place.
_CANDIDATES = text(f"""
    SELECT s.id, s.tmdb_id, s.name
      FROM catalog.show s
     WHERE {_NEEDS_CREDITS}
       AND s.id > :after_id
     ORDER BY s.id
     LIMIT :limit
""")

_REMAINING = text(f"SELECT count(*) FROM catalog.show s WHERE {_NEEDS_CREDITS}")


async def backfill_show_credits(
    session: AsyncSession, client: TMDBClient, show: ShowToBackfill
) -> bool:
    """Write one show's credits and stamp it. Returns whether upstream had any.

    Fetches through the ingest's own `fetch_series_with_seasons`, with the full
    `DEFAULT_APPEND`, so the payload is the one the ingest would have written —
    credits at both grains, and a show whose seasons overflow the append budget
    fetched completely rather than in part. An overflow fetch that fails takes
    the show down rather than writing half of it, which is the behaviour that
    function already owns and the reason the stamp is safe to trust afterwards.

    A payload arriving without `aggregate_credits` raises rather than stamping:
    the namespace is always appended, so its absence is a statement about the
    response and not about the series. See `MissingCreditsNamespace`.

    Does not commit: the caller owns the transaction, so the credit rows and the
    watermark land together or not at all.
    """
    series, overflow = await fetch_series_with_seasons(client, show.tmdb_id)
    if series.aggregate_credits is None:
        raise MissingCreditsNamespace(
            f"series {show.tmdb_id} came back without aggregate_credits — not stamping"
        )
    had_credits = await write_series_credits(session, series, show_id=show.id, seasons=overflow)
    await mark_credits_synced(session, show_id=show.id)
    return had_credits


async def backfill_credits(
    session: AsyncSession,
    client: TMDBClient,
    *,
    limit: int | None = None,
    page_size: int = _PAGE_SIZE,
    failure_threshold: int = _FAILURE_THRESHOLD,
    progress_every: int = _PROGRESS_EVERY,
) -> CreditsBackfillResult:
    """Write credits for every mirrored show that has none, committing per show.

    Owns its transaction boundaries for the reason `enrich_show_ids` does: this
    is hours of upstream calls, and work that cannot be reproduced for free has
    to be committed as it is done.

    A per-show failure is counted and stepped over — one broken series must not
    cost the pass — but `failure_threshold` consecutive real failures raise
    `CreditsBackfillAborted`, because at that point the upstream is down rather
    than the data being odd. A 404 is neither: the show stays unstamped, so a
    later run will try it again, and if TMDB has genuinely deleted the series the
    tombstone pass is what settles it.

    `limit` caps how many shows are considered, which is how to try a hundred
    before committing to the full pass.
    """
    total = (await session.execute(_REMAINING)).scalar_one()
    log.info(
        "credits backfill: %d mirrored show(s) have no credits%s",
        total,
        f", considering {limit}" if limit else "",
    )

    after_id = 0
    considered = 0
    stamped = 0
    without_credits = 0
    failed = 0
    gone = 0
    consecutive_failures = 0

    def _log_progress() -> None:
        log.info(
            "credits backfill: %d/%d considered — %d written (%d with no credits upstream), "
            "%d failed (%d gone upstream, %d real)",
            considered,
            total,
            stamped,
            without_credits,
            failed,
            gone,
            failed - gone,
        )

    while limit is None or considered < limit:
        take = page_size if limit is None else min(page_size, limit - considered)
        rows = (await session.execute(_CANDIDATES, {"after_id": after_id, "limit": take})).all()
        if not rows:
            break

        for row in rows:
            show = ShowToBackfill(id=row.id, tmdb_id=row.tmdb_id, name=row.name)
            after_id = show.id
            considered += 1
            try:
                had_credits = await backfill_show_credits(session, client, show)
                await session.commit()
            except Exception as exc:
                # The show's partial writes must go, or a re-run would find rows
                # from a payload that never fully landed.
                await session.rollback()
                failed += 1
                if is_gone_upstream(exc):
                    gone += 1
                    log.info(
                        "show %d (%s): TMDB %d is gone upstream — left unstamped",
                        show.id,
                        show.name,
                        show.tmdb_id,
                    )
                    continue
                consecutive_failures += 1
                if isinstance(exc, httpx.HTTPStatusError):
                    # Retried to exhaustion by the client already, so this is a
                    # persistent upstream failure rather than a bug here — the
                    # status line is the whole story and a traceback is noise.
                    log.warning(
                        "show %d (%s): credits backfill failed: %s", show.id, show.name, exc
                    )
                else:
                    # Anything else is ours, and the same distinction
                    # `mirror_series` draws: without the traceback a bug in this
                    # loop reads as an upstream problem.
                    log.exception("show %d (%s): unexpected credits error", show.id, show.name)
                if consecutive_failures >= failure_threshold:
                    _log_progress()
                    raise CreditsBackfillAborted(
                        f"aborted after {consecutive_failures} consecutive failures: {exc}"
                    ) from exc
                continue

            consecutive_failures = 0
            stamped += 1
            if not had_credits:
                without_credits += 1
            if considered % progress_every == 0:
                _log_progress()

    _log_progress()
    return CreditsBackfillResult(
        shows_considered=considered,
        shows_stamped=stamped,
        shows_failed=failed,
        shows_gone=gone,
        shows_without_credits=without_credits,
    )


# --- the report -------------------------------------------------------------
#
# Read live rather than written to `docs/migration/`, for the same reason
# NEU-1045's is: a snapshot is stale the moment somebody re-runs the pass. It
# needs no TMDB credential and writes nothing, so it is safe to run against
# production before the pass is spent — which is the point, because the numbers
# it returns beforehand are the ticket's evidence and afterwards are its proof.

_TABLE_COUNTS = text("""
    SELECT (SELECT count(*) FROM catalog.show_cast)           AS show_cast,
           (SELECT count(*) FROM catalog.show_crew)           AS show_crew,
           (SELECT count(*) FROM catalog.episode_guest_cast)  AS episode_guest_cast,
           (SELECT count(*) FROM catalog.episode_crew)        AS episode_crew,
           (SELECT count(*) FROM catalog.person)              AS person,
           (SELECT count(*) FROM catalog."character")         AS character,
           (SELECT count(*) FROM catalog.crew_role)           AS crew_role
""")

_TOTALS = text(f"""
    SELECT count(*) FILTER (WHERE s.tmdb_synced_at IS NOT NULL)   AS shows_mirrored,
           count(*) FILTER (WHERE s.credits_synced_at IS NOT NULL) AS shows_stamped,
           count(*) FILTER (WHERE {_NEEDS_CREDITS})                AS shows_remaining,
           count(*) FILTER (
               WHERE s.credits_synced_at IS NOT NULL
                 AND NOT EXISTS (SELECT 1 FROM catalog.show_cast c WHERE c.show_id = s.id)
                 AND NOT EXISTS (SELECT 1 FROM catalog.show_crew c WHERE c.show_id = s.id)
           )                                                       AS stamped_without_show_credits
      FROM catalog.show s
""")

# The acceptance criterion a person actually checks: a show somebody has on their
# list *or* has watched an episode of, that still serves an empty cast list.
# Both, because the two disagree — a show can carry watch history without a My
# Shows row — and a spot-check list that quietly covered one of them would be
# the report claiming a reach it does not have.
#
# Worst first, because that is the order somebody spot-checking would want them
# in, and capped, because before the pass runs this is every tracked show and
# the artifact would be hundreds of rows saying one thing.
_USER_TOUCHED = """
    SELECT show_id, user_id FROM app.user_show_watch
     UNION
    SELECT e.show_id, w.user_id
      FROM app.user_episode_watch w
      JOIN catalog.episode e ON e.id = w.episode_id
"""

_HAS_NO_CAST = """
    NOT EXISTS (SELECT 1 FROM catalog.show_cast c WHERE c.show_id = s.id)
"""

_USER_TOUCHED_WITHOUT_CREDITS = text(f"""
    WITH touched AS ({_USER_TOUCHED})
    SELECT s.id AS show_id,
           s.name AS name,
           s.tmdb_id AS tmdb_id,
           s.match_method AS match_method,
           count(DISTINCT t.user_id) AS users,
           s.credits_synced_at IS NOT NULL AS stamped
      FROM touched t
      JOIN catalog.show s ON s.id = t.show_id
     WHERE {_HAS_NO_CAST}
     GROUP BY s.id, s.name, s.tmdb_id, s.match_method, s.credits_synced_at
     ORDER BY count(DISTINCT t.user_id) DESC, s.id
     LIMIT :limit
""")

# How many there are in total, since the list above is capped.
_USER_TOUCHED_WITHOUT_CREDITS_TOTAL = text(f"""
    SELECT count(*)
      FROM (SELECT DISTINCT show_id FROM ({_USER_TOUCHED}) u) t
      JOIN catalog.show s ON s.id = t.show_id
     WHERE {_HAS_NO_CAST}
""")

_REPORT_LIST_LIMIT = 50


# The seven tables the pass is allowed to write, and the only ones worth
# counting: the four credit tables plus the three lookups they intern into.
_COUNTED_TABLES = (
    "show_cast",
    "show_crew",
    "episode_guest_cast",
    "episode_crew",
    "person",
    "character",
    "crew_role",
)


@dataclass(frozen=True)
class CreditsBackfillReport:
    totals: dict[str, int]
    table_counts: dict[str, int]
    user_touched_without_credits: list[dict[str, Any]]
    user_touched_without_credits_total: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "totals": self.totals,
            "table_counts": self.table_counts,
            "user_touched_without_credits_total": self.user_touched_without_credits_total,
            "user_touched_without_credits": self.user_touched_without_credits,
        }


async def build_report(
    session: AsyncSession, *, limit: int = _REPORT_LIST_LIMIT
) -> CreditsBackfillReport:
    """What the credit tables hold, what is left to fetch, and who is still empty.

    Every field is named rather than handed over as whatever the SELECT list
    happened to be, for the reason `catalog/schemas.py` names its builders'
    fields: this artifact is diffed against a saved copy either side of a
    production run, so a column silently entering or changing type turns a
    regression check into noise.
    """
    totals = (await session.execute(_TOTALS)).one()
    table_counts = (await session.execute(_TABLE_COUNTS)).one()
    rows = (await session.execute(_USER_TOUCHED_WITHOUT_CREDITS, {"limit": limit})).all()
    user_touched_total = (await session.execute(_USER_TOUCHED_WITHOUT_CREDITS_TOTAL)).scalar_one()

    return CreditsBackfillReport(
        totals={
            "shows_mirrored": totals.shows_mirrored,
            "shows_stamped": totals.shows_stamped,
            "shows_remaining": totals.shows_remaining,
            "stamped_without_show_credits": totals.stamped_without_show_credits,
        },
        table_counts={name: getattr(table_counts, name) for name in _COUNTED_TABLES},
        user_touched_without_credits=[
            {
                "show_id": row.show_id,
                "name": row.name,
                "tmdb_id": row.tmdb_id,
                "match_method": row.match_method,
                "users": row.users,
                "stamped": row.stamped,
            }
            for row in rows
        ],
        user_touched_without_credits_total=user_touched_total,
    )
