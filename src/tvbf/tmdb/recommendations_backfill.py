"""Fill `catalog.show_recommendation` for shows mirrored before the namespace existed (NEU-1052).

## What happened, and why it is not the credits backfill again

It is the same shape and a different cause. NEU-1127's backlog existed because
the writers merged after the pass that fetched their data; this one exists
because the *request* did not carry the namespace at all. NEU-1031 classified
`recommendations` as skipped — "TMDB-computed and volatile rather than a catalog
fact" — and the same paragraph warned that "every namespace left off is a field
that becomes a multi-hour backfill later". All 229,418 mirrored shows are that
warning coming true, on schedule.

`recommendations` is in `DEFAULT_APPEND` from NEU-1052 on, so **this is the last
time**: every future ingest and every nightly delta refreshes a show's list as a
side effect of the fetch it already makes. There is no recurring refresh pass, no
cadence to choose and no staleness rule.

## Why re-running the ingest is not the fix

Its work list is `tmdb_synced_at IS NULL` and every one of those rows is stamped
— the shut window NEU-1045 hit and NEU-1127 hit again. Clearing the stamp costs
the same requests and rewrites 229k shows and 6.5M episodes to fill one table,
over a spine users are now reading from. So this pass asks for the one namespace
it needs and writes through `upsert.write_series_recommendations`, the seam that
makes "writes nothing else" a property of the writer rather than a promise made
by the caller.

## One request per show, and it is narrower than the ingest's

The append list here is `("recommendations",)` and nothing else. The ingest
appends twelve namespaces plus a speculative season window because it is
mirroring the show; this pass needs one page of twenty ids, so a show with forty
seasons costs exactly one request rather than one plus the overflow. ~8.8 hours
for the full catalog at the measured 7.27 shows/sec, latency-bound like every
other sequential pass here rather than budget-bound.

## Ordered by popularity, and stopping early is a supported outcome

The work list is taken **`popularity DESC`**, which is what front-loads the
value: the top 20,000 shows take about 45 minutes and cover essentially every
page a user will load. A pass killed at any point leaves a mirror whose popular
shows have a "More like this" section and whose long tail does not — and because
the watermark makes a partial pass indistinguishable from a paused one, resuming
is just running it again. The surface degrades to *no section at all* for shows
nobody visits, which is the project spec's own degradation rule rather than a
compromise.

`catalog.show.popularity` is refreshed nightly from the daily export (NEU-1172),
so the ordering reflects this week rather than the ingest's vintage. That is the
whole reason this ticket is blocked on that one.

## The watermark is a column, for the reason it was the last two times

`recommendations_synced_at`, not "the show has no `show_recommendation` row".
Upstream returning nothing is a normal outcome here and a common one — ~8% of
the zero-vote long tail — and the row-existence predicate cannot tell it from
*nobody has asked*, so every one of those shows would be re-fetched on every run
and the pass would never converge.
"""

import logging
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.tmdb.api_payloads import TMDBSeries
from tvbf.tmdb.client import TMDBClient, is_gone_upstream
from tvbf.tmdb.upsert import (
    RecommendationsWritten,
    mark_recommendations_synced,
    write_series_recommendations,
)

log = logging.getLogger(__name__)

# The only namespace this pass asks for. Deliberately not `DEFAULT_APPEND`: it
# writes one table, and appending the other eleven would fetch a payload it
# throws away and drag the speculative season window along with it.
APPEND: tuple[str, ...] = ("recommendations",)

# How many shows one candidate query takes. Not a commit boundary — this pass
# commits per show, as `credits_backfill` does, because an interrupt should cost
# at most one show's request. Larger than that pass's page because each query
# here sorts the show table on an unindexed expression (see `_CANDIDATES`), and
# a page is one such sort.
_PAGE_SIZE = 500

# Consecutive per-show failures before the pass gives up, the threshold every
# other TMDB pass uses. A 404 does not count.
_FAILURE_THRESHOLD = 10

# How often to log a running total — a line every ~2 minutes at the measured
# rate, the cadence every multi-hour pass here uses.
_PROGRESS_EVERY = 1000


class RecommendationsBackfillAborted(Exception):
    """Too many consecutive per-show failures. Ends the pass; the log has the rest."""


class MissingRecommendationsNamespace(Exception):
    """The payload came back without the `recommendations` we asked for.

    A per-show failure rather than "this show has none", on
    `MissingCreditsNamespace`'s reasoning: the request always appends the
    namespace, so its absence describes the response and not the series.
    Stamping on it would retire the show from the backlog having never seen its
    list, which is the silent partial this whole pass exists to repair.
    """


@dataclass(frozen=True)
class ShowToBackfill:
    """A mirrored show whose recommendations have not been written."""

    id: int
    tmdb_id: int
    name: str
    # The ordering key this row was taken at, carried back out so the next page
    # can resume from it. `coalesce(popularity, -1)`, never the raw column.
    sort_popularity: float


@dataclass(frozen=True)
class RecommendationsBackfillResult:
    shows_considered: int
    shows_stamped: int
    shows_failed: int
    shows_gone: int
    # Stamped, but **upstream** recommended nothing. Counted apart from
    # `shows_stamped` because it is the outcome a reader would otherwise mistake
    # for a failure — and because it is ~8% of the long tail, so a *rising* share
    # of it among popular shows is the one sign the namespace has stopped
    # answering. That signal only works if this counts upstream's silence and
    # nothing else, which is why a show whose twenty entries all failed to
    # resolve lands in `targets_dropped` instead.
    shows_without_recommendations: int
    rows_written: int
    # Entries upstream offered that resolved to no `catalog.show` — the other way
    # a stamped show ends up with no rows, and a different problem: a show TMDB
    # created this morning is not mirrored until tonight's delta, so a large
    # number here says the mirror is behind rather than that upstream is quiet.
    targets_dropped: int


# What makes a show worth a request: a complete payload has been mirrored onto it
# and no pass has written its recommendations. Written once and shared by the
# work list and its count, so the denominator in the progress log cannot drift
# from what the loop actually takes.
#
# `tmdb_id IS NOT NULL` is implied by `tmdb_synced_at` and restated anyway: it is
# the id the request is made with, and a locally-authored row (ADR-0008) has none
# to make it with.
_NEEDS_RECOMMENDATIONS = """
    s.tmdb_id IS NOT NULL
    AND s.tmdb_synced_at IS NOT NULL
    AND s.recommendations_synced_at IS NULL
"""

# Keyset paging on the ordering key rather than OFFSET, for the reason every
# other pass here uses one: a show leaves the candidate set the moment it is
# stamped, so an offset would step over whatever slid into its place. The key is
# `(coalesce(popularity, -1), id)` descending — a two-part key because
# `popularity` is not unique and a one-part cursor would skip or repeat its ties,
# and `coalesce` because NULL popularity (a show the export has never carried a
# score for) must sort last rather than out.
#
# A show that *failed* keeps its watermark and stays in the work list, which is
# exactly why the cursor is needed at all: without it the next page would hand
# back the same failing show forever.
#
# **The ordering key is mutable, unlike `credits_backfill`'s primary key**, and
# that is a property to know rather than a bug to fix: NEU-1172 rewrites
# `popularity` nightly from the daily export, so a show whose score rises past a
# descending cursor mid-pass is stepped over for the rest of *that* run. It keeps
# its null watermark and the next run takes it, which is the same recovery a
# failed show gets — so a single run is not guaranteed to empty the work list,
# and the pass is written to be run again rather than to be run once. Ordering on
# an immutable key instead would cost the popularity ordering, which is the whole
# reason a partial pass is worth anything.
_CANDIDATES = text(f"""
    SELECT s.id, s.tmdb_id, s.name, coalesce(s.popularity, -1) AS sort_popularity
      FROM catalog.show s
     WHERE {_NEEDS_RECOMMENDATIONS}
       AND (coalesce(s.popularity, -1), s.id) < (:after_popularity, :after_id)
     ORDER BY coalesce(s.popularity, -1) DESC, s.id DESC
     LIMIT :limit
""")

_REMAINING = text(f"SELECT count(*) FROM catalog.show s WHERE {_NEEDS_RECOMMENDATIONS}")

# The cursor's starting point: above every popularity and every id, so the first
# page is the head of the list. `float("inf")` is a real double to Postgres and
# compares as one.
_START_CURSOR = (float("inf"), 2**63 - 1)


async def backfill_show_recommendations(
    session: AsyncSession, client: TMDBClient, show: ShowToBackfill
) -> RecommendationsWritten:
    """Write one show's recommendations and stamp it. Returns what was written and offered.

    One request, `APPEND` and nothing else. A payload arriving without the
    namespace raises rather than stamping — see `MissingRecommendationsNamespace`.

    Does not commit: the caller owns the transaction, so the rows and the
    watermark land together or not at all.
    """
    payload = await client.get_tv_series(show.tmdb_id, append=list(APPEND))
    series = TMDBSeries.model_validate(payload)
    if series.recommendations is None:
        raise MissingRecommendationsNamespace(
            f"series {show.tmdb_id} came back without recommendations — not stamping"
        )
    written = await write_series_recommendations(session, series, show_id=show.id)
    await mark_recommendations_synced(session, show_id=show.id)
    return written


async def backfill_recommendations(
    session: AsyncSession,
    client: TMDBClient,
    *,
    limit: int | None = None,
    page_size: int = _PAGE_SIZE,
    failure_threshold: int = _FAILURE_THRESHOLD,
    progress_every: int = _PROGRESS_EVERY,
) -> RecommendationsBackfillResult:
    """Write recommendations for every mirrored show that has none, most popular first.

    Owns its transaction boundaries for the reason every hours-long pass here
    does: work that cannot be reproduced for free has to be committed as it is
    done.

    A per-show failure is counted and stepped over — one broken series must not
    cost the pass — but `failure_threshold` consecutive real failures raise
    `RecommendationsBackfillAborted`, because at that point upstream is down
    rather than the data being odd. A 404 is neither: the show stays unstamped,
    so a later run tries it again, and if TMDB has genuinely deleted the series
    the tombstone pass is what settles it.

    `limit` caps how many shows are considered. It is the smoke-run control, and
    it is also how a deliberately partial pass is run — the top N by popularity
    is a supported destination, not a truncated journey.
    """
    total = (await session.execute(_REMAINING)).scalar_one()
    log.info(
        "recommendations backfill: %d mirrored show(s) have none%s",
        total,
        f", considering {limit}" if limit else "",
    )

    after_popularity, after_id = _START_CURSOR
    considered = 0
    stamped = 0
    without = 0
    rows_written = 0
    dropped = 0
    failed = 0
    gone = 0
    consecutive_failures = 0

    def _log_progress() -> None:
        log.info(
            "recommendations backfill: %d/%d considered — %d stamped (%d row(s) written, "
            "%d target(s) dropped as unmirrored, %d show(s) with none upstream), "
            "%d failed (%d gone upstream, %d real)",
            considered,
            total,
            stamped,
            rows_written,
            dropped,
            without,
            failed,
            gone,
            failed - gone,
        )

    while limit is None or considered < limit:
        take = page_size if limit is None else min(page_size, limit - considered)
        rows = (
            await session.execute(
                _CANDIDATES,
                {"after_popularity": after_popularity, "after_id": after_id, "limit": take},
            )
        ).all()
        if not rows:
            break

        for row in rows:
            show = ShowToBackfill(
                id=row.id,
                tmdb_id=row.tmdb_id,
                name=row.name,
                sort_popularity=row.sort_popularity,
            )
            after_popularity, after_id = show.sort_popularity, show.id
            considered += 1
            try:
                result = await backfill_show_recommendations(session, client, show)
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
                        "show %d (%s): recommendations backfill failed: %s",
                        show.id,
                        show.name,
                        exc,
                    )
                else:
                    # Anything else is ours, and the same distinction
                    # `mirror_series` draws: without the traceback a bug in this
                    # loop reads as an upstream problem.
                    log.exception(
                        "show %d (%s): unexpected recommendations error", show.id, show.name
                    )
                if consecutive_failures >= failure_threshold:
                    _log_progress()
                    raise RecommendationsBackfillAborted(
                        f"aborted after {consecutive_failures} consecutive failures: {exc}"
                    ) from exc
                continue

            consecutive_failures = 0
            stamped += 1
            rows_written += result.written
            dropped += result.dropped
            # Upstream's silence, not ours — a show whose entries all failed to
            # resolve is counted in `dropped` and is a different problem.
            if not result.offered:
                without += 1
            if considered % progress_every == 0:
                _log_progress()

    _log_progress()
    return RecommendationsBackfillResult(
        shows_considered=considered,
        shows_stamped=stamped,
        shows_failed=failed,
        shows_gone=gone,
        shows_without_recommendations=without,
        rows_written=rows_written,
        targets_dropped=dropped,
    )


# --- the report -------------------------------------------------------------
#
# Read live rather than written to a file under `docs/`, for the reason
# NEU-1127's is: a snapshot is stale the moment somebody re-runs the pass. It
# needs no TMDB credential and writes nothing, so it is safe to run against
# production before the pass is spent — which is the point, because the numbers
# it returns beforehand are the ticket's evidence and afterwards are its proof.

# `stamped_without_rows` is honest about what it counts — a stamped show holding
# no rows — and deliberately does not try to say *why*. The two causes are
# upstream having none and every target being unmirrored, and no query over the
# stored state can separate them after the fact; the pass's own `targets_dropped`
# is where that distinction is drawn, while it still has the payload in hand.
_TOTALS = text(f"""
    SELECT count(*) FILTER (WHERE s.tmdb_synced_at IS NOT NULL)            AS shows_mirrored,
           count(*) FILTER (WHERE s.recommendations_synced_at IS NOT NULL) AS shows_stamped,
           count(*) FILTER (WHERE {_NEEDS_RECOMMENDATIONS})                AS shows_remaining,
           count(*) FILTER (
               WHERE s.recommendations_synced_at IS NOT NULL
                 AND NOT EXISTS (
                     SELECT 1 FROM catalog.show_recommendation r WHERE r.source_show_id = s.id
                 )
           )                                                               AS stamped_without_rows
      FROM catalog.show s
""")

_ROW_TOTALS = text("""
    SELECT count(*)                        AS rows_stored,
           count(DISTINCT source_show_id)  AS source_shows,
           count(DISTINCT target_show_id)  AS target_shows
      FROM catalog.show_recommendation
""")

# The one integrity question the storage rule makes, asked of the stored rows
# rather than of the writer: a target that has since been tombstoned. Non-zero is
# not a defect — the read path filters `deleted_upstream_at` for exactly this,
# and the source show's next refresh drops the row — but a *large* number would
# mean refreshes have stopped happening at all.
_STALE_TARGETS = text("""
    SELECT count(*)
      FROM catalog.show_recommendation r
      JOIN catalog.show t ON t.id = r.target_show_id
     WHERE t.deleted_upstream_at IS NOT NULL
""")

# The acceptance criterion a person actually checks: a show somebody tracks or
# has watched an episode of that would still render no "More like this" section.
# Both, because the two disagree — a show can carry watch history without a My
# Shows row.
#
# Worst first, because that is the order somebody spot-checking wants them in,
# and capped, because before the pass runs this is every tracked show and the
# artifact would be hundreds of rows saying one thing.
_USER_TOUCHED = """
    SELECT show_id, user_id FROM app.user_show_watch
     UNION
    SELECT e.show_id, w.user_id
      FROM app.user_episode_watch w
      JOIN catalog.episode e ON e.id = w.episode_id
"""

_HAS_NO_RECOMMENDATIONS = """
    NOT EXISTS (SELECT 1 FROM catalog.show_recommendation r WHERE r.source_show_id = s.id)
"""

_USER_TOUCHED_WITHOUT_RECOMMENDATIONS = text(f"""
    WITH touched AS ({_USER_TOUCHED})
    SELECT s.id AS show_id,
           s.name AS name,
           s.tmdb_id AS tmdb_id,
           s.popularity AS popularity,
           count(DISTINCT t.user_id) AS users,
           s.recommendations_synced_at IS NOT NULL AS stamped
      FROM touched t
      JOIN catalog.show s ON s.id = t.show_id
     WHERE {_HAS_NO_RECOMMENDATIONS}
     GROUP BY s.id, s.name, s.tmdb_id, s.popularity, s.recommendations_synced_at
     ORDER BY count(DISTINCT t.user_id) DESC, s.popularity DESC NULLS LAST, s.id
     LIMIT :limit
""")

_USER_TOUCHED_WITHOUT_RECOMMENDATIONS_TOTAL = text(f"""
    SELECT count(*)
      FROM (SELECT DISTINCT show_id FROM ({_USER_TOUCHED}) u) t
      JOIN catalog.show s ON s.id = t.show_id
     WHERE {_HAS_NO_RECOMMENDATIONS}
""")

_REPORT_LIST_LIMIT = 50


@dataclass(frozen=True)
class RecommendationsReport:
    totals: dict[str, int]
    rows: dict[str, int]
    targets_tombstoned: int
    user_touched_without_recommendations: list[dict[str, Any]]
    user_touched_without_recommendations_total: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "totals": self.totals,
            "rows": self.rows,
            "targets_tombstoned": self.targets_tombstoned,
            "user_touched_without_recommendations": self.user_touched_without_recommendations,
            "user_touched_without_recommendations_total": (
                self.user_touched_without_recommendations_total
            ),
        }


async def build_report(
    session: AsyncSession, *, list_limit: int = _REPORT_LIST_LIMIT
) -> RecommendationsReport:
    """What the table holds, what is left, and which tracked shows still have nothing.

    Every field is named rather than handed over as whatever the SELECT list
    happened to be, on `credits_backfill.build_report`'s reasoning: this artifact
    is diffed against a saved copy either side of a production run, so a column
    silently entering or changing type turns a regression check into noise.
    """
    totals = (await session.execute(_TOTALS)).one()
    rows = (await session.execute(_ROW_TOTALS)).one()
    tombstoned = (await session.execute(_STALE_TARGETS)).scalar_one()
    listed = (
        await session.execute(_USER_TOUCHED_WITHOUT_RECOMMENDATIONS, {"limit": list_limit})
    ).all()
    listed_total = (await session.execute(_USER_TOUCHED_WITHOUT_RECOMMENDATIONS_TOTAL)).scalar_one()

    return RecommendationsReport(
        totals={
            "shows_mirrored": totals.shows_mirrored,
            "shows_stamped": totals.shows_stamped,
            "shows_remaining": totals.shows_remaining,
            "stamped_without_rows": totals.stamped_without_rows,
        },
        rows={
            "rows_stored": rows.rows_stored,
            "source_shows": rows.source_shows,
            "target_shows": rows.target_shows,
        },
        targets_tombstoned=tombstoned,
        user_touched_without_recommendations=[
            {
                "show_id": row.show_id,
                "name": row.name,
                "tmdb_id": row.tmdb_id,
                "popularity": row.popularity,
                "users": row.users,
                "stamped": row.stamped,
            }
            for row in listed
        ],
        user_touched_without_recommendations_total=listed_total,
    )
