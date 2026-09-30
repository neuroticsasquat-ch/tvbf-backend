"""Fill `catalog.season_cast` for shows mirrored before season credits were fetched (NEU-1512).

## What is missing

TMDB records a show's regular cast **per season** — `season/{n}/credits` — and
the catalog never fetched it: NEU-1031 skipped `credits` as "strictly weaker
than `aggregate_credits`", which is true at show grain, and the season-grain
list went unfetched with it. So every show mirrored before NEU-1512 holds its
regulars once, as `show_cast` rows with no season attached, and
`catalog.season_cast` is empty behind it.

From NEU-1512 on, `fetch_series_with_seasons` asks for every season's credits
alongside its episodes, so the full pass and the daily delta keep
`season_cast` current as a side effect. This pass is the one-time cost of the
backlog, and it is `credits_backfill.py` with the nouns changed, because that
shape has run over the whole catalog once and its properties are the ones
wanted: a column watermark, keyset paging, one commit per show, 404s apart
from real failures, and an abort after ten consecutive real ones.

## The request is the ingest's, minus the namespaces

Each show is fetched through `fetch_series_with_seasons` with **no
namespaces** — the shape NEU-1045's episode mapping uses. The pass writes only
`season_cast`, so the twelve namespaces would be bytes nobody reads, and giving
their slots back widens the season window from four seasons to ten: ~97% of
shows then cost one request, where the full `DEFAULT_APPEND` would send ~90k
overflow requests after the first. Every season still arrives with its
`credits`, appended or standalone, because that pairing lives in the fetch.

## The watermark is a column, for the reason every such pass has one

"The show has no `season_cast` row" cannot tell *upstream lists no regulars*
— common on small shows, and anthology or animated formats — from *nobody has
asked*, so it would re-fetch those forever. `season_credits_synced_at` is
stamped whether or not upstream listed anyone, and `mark_series_synced` stamps
it too, so a show the delta has covered never enters the backlog.

A show is stamped only when **every** season came back with a `credits` key.
One that did not is counted and left unstamped, on `MissingCreditsNamespace`'s
reasoning: the request always asks for it, so its absence describes the
response rather than the season, and an unstamped show is retried where a
stamped one is believed.
"""

import logging
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.tmdb.client import TMDBClient, is_gone_upstream
from tvbf.tmdb.ingest import fetch_series_with_seasons
from tvbf.tmdb.upsert import mark_season_credits_synced, write_season_credits

log = logging.getLogger(__name__)

# Candidates per keyset page. Not a commit boundary — the pass commits per
# show, for `credits_backfill._PAGE_SIZE`'s reasons.
_PAGE_SIZE = 200

# Consecutive real per-show failures before the pass gives up, the threshold
# every other TMDB pass uses. A 404 does not count.
_FAILURE_THRESHOLD = 10

# A running total every thousand shows — the cadence the other passes use.
_PROGRESS_EVERY = 1000


class SeasonCreditsBackfillAborted(Exception):
    """Too many consecutive per-show failures. Ends the pass; the log has the rest."""


class MissingSeasonCredits(Exception):
    """A season came back without the `credits` the request asked for.

    A per-show failure rather than "this season has no regulars", because
    `cast: []` is how upstream says that. Stamping on it would retire the show
    from the backlog having never seen that season's regulars.
    """

    def __init__(self, tmdb_id: int, seasons: int):
        super().__init__(
            f"series {tmdb_id}: {seasons} season(s) came back without credits — not stamping"
        )
        self.seasons = seasons


@dataclass(frozen=True)
class ShowToBackfill:
    """A mirrored show whose season regulars have not been written."""

    id: int
    tmdb_id: int
    name: str


@dataclass(frozen=True)
class SeasonCreditsBackfillResult:
    shows_considered: int
    shows_stamped: int
    shows_failed: int
    shows_gone: int
    # Seasons whose `season_cast` was replaced, across every stamped show.
    seasons_written: int
    # Seasons that came back with no `credits` key, across every show that was
    # therefore left unstamped. Rising is the sign the append stopped working.
    seasons_without_credits: int


# Written once and shared by the work list and its count, so the progress
# denominator cannot drift from what the loop takes. `tmdb_id IS NOT NULL` is
# restated for `credits_backfill._NEEDS_CREDITS`' reason.
_NEEDS_SEASON_CREDITS = """
    s.tmdb_id IS NOT NULL
    AND s.tmdb_synced_at IS NOT NULL
    AND s.season_credits_synced_at IS NULL
"""

# Keyset rather than OFFSET: a show leaves the candidate set when it is stamped.
_CANDIDATES = text(f"""
    SELECT s.id, s.tmdb_id, s.name
      FROM catalog.show s
     WHERE {_NEEDS_SEASON_CREDITS}
       AND s.id > :after_id
     ORDER BY s.id
     LIMIT :limit
""")

_REMAINING = text(f"SELECT count(*) FROM catalog.show s WHERE {_NEEDS_SEASON_CREDITS}")


async def backfill_show_season_credits(
    session: AsyncSession, client: TMDBClient, show: ShowToBackfill
) -> int:
    """Write one show's season regulars and stamp it. Returns the seasons written.

    Raises `MissingSeasonCredits` rather than stamping when any season came
    back without its `credits`. Does not commit: the caller owns the
    transaction, so the rows and the watermark land together or not at all.
    """
    series, overflow = await fetch_series_with_seasons(client, show.tmdb_id, namespaces=())
    written = await write_season_credits(session, series, show_id=show.id, seasons=overflow)
    if written.seasons_without_credits:
        raise MissingSeasonCredits(show.tmdb_id, written.seasons_without_credits)
    await mark_season_credits_synced(session, show_id=show.id)
    return written.seasons_written


async def backfill_season_credits(
    session: AsyncSession,
    client: TMDBClient,
    *,
    limit: int | None = None,
    page_size: int = _PAGE_SIZE,
    failure_threshold: int = _FAILURE_THRESHOLD,
    progress_every: int = _PROGRESS_EVERY,
) -> SeasonCreditsBackfillResult:
    """Write season regulars for every mirrored show without them, committing per show.

    The failure semantics are `credits_backfill.backfill_credits`': a failure is
    counted and stepped over, `failure_threshold` consecutive real ones raise
    `SeasonCreditsBackfillAborted`, and a 404 is neither — the show stays
    unstamped for a later run or the tombstone pass to settle.

    `limit` caps how many shows are considered, for a smoke run.
    """
    total = (await session.execute(_REMAINING)).scalar_one()
    log.info(
        "season credits backfill: %d mirrored show(s) have no season regulars written%s",
        total,
        f", considering {limit}" if limit else "",
    )

    after_id = 0
    considered = 0
    stamped = 0
    failed = 0
    gone = 0
    seasons_written = 0
    seasons_without_credits = 0
    consecutive_failures = 0

    def _log_progress() -> None:
        log.info(
            "season credits backfill: %d/%d considered — %d written (%d seasons), "
            "%d failed (%d gone upstream, %d real; %d seasons without credits)",
            considered,
            total,
            stamped,
            seasons_written,
            failed,
            gone,
            failed - gone,
            seasons_without_credits,
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
                written = await backfill_show_season_credits(session, client, show)
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
                if isinstance(exc, MissingSeasonCredits):
                    seasons_without_credits += exc.seasons
                    log.warning("show %d (%s): %s", show.id, show.name, exc)
                elif isinstance(exc, httpx.HTTPStatusError):
                    log.warning(
                        "show %d (%s): season credits backfill failed: %s", show.id, show.name, exc
                    )
                else:
                    log.exception(
                        "show %d (%s): unexpected season credits error", show.id, show.name
                    )
                if consecutive_failures >= failure_threshold:
                    _log_progress()
                    raise SeasonCreditsBackfillAborted(
                        f"aborted after {consecutive_failures} consecutive failures: {exc}"
                    ) from exc
                continue

            consecutive_failures = 0
            stamped += 1
            seasons_written += written
            if considered % progress_every == 0:
                _log_progress()

    _log_progress()
    return SeasonCreditsBackfillResult(
        shows_considered=considered,
        shows_stamped=stamped,
        shows_failed=failed,
        shows_gone=gone,
        seasons_written=seasons_written,
        seasons_without_credits=seasons_without_credits,
    )


# --- the report -------------------------------------------------------------
#
# Read live, needs no TMDB credential and writes nothing — safe against
# production before, during and after the pass. `shows_remaining` reaching zero
# is what PR 2 of NEU-1512 waits for.

_TOTALS = text(f"""
    SELECT count(*) FILTER (WHERE s.tmdb_synced_at IS NOT NULL)          AS shows_mirrored,
           count(*) FILTER (WHERE s.season_credits_synced_at IS NOT NULL) AS shows_stamped,
           count(*) FILTER (WHERE {_NEEDS_SEASON_CREDITS})                AS shows_remaining,
           count(*) FILTER (
               WHERE s.season_credits_synced_at IS NOT NULL
                 AND EXISTS (SELECT 1 FROM catalog.show_cast c WHERE c.show_id = s.id)
                 AND NOT EXISTS (
                     SELECT 1
                       FROM catalog.season_cast sc
                       JOIN catalog.season se ON se.id = sc.season_id
                      WHERE se.show_id = s.id
                 )
           ) AS stamped_with_no_regulars
      FROM catalog.show s
""")

_SEASON_CAST_ROWS = text("SELECT count(*) FROM catalog.season_cast")


@dataclass(frozen=True)
class SeasonCreditsBackfillReport:
    shows_mirrored: int
    shows_stamped: int
    shows_remaining: int
    # Stamped, carrying show cast, and holding no regular in any season: the
    # "TMDB lists no regulars" population. Informational, and expected to be
    # non-trivial for small shows — on the show page every one of their cast
    # reads as a guest.
    stamped_with_no_regulars: int
    season_cast_rows: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "shows_mirrored": self.shows_mirrored,
            "shows_stamped": self.shows_stamped,
            "shows_remaining": self.shows_remaining,
            "stamped_with_no_regulars": self.stamped_with_no_regulars,
            "season_cast_rows": self.season_cast_rows,
        }


async def build_report(session: AsyncSession) -> SeasonCreditsBackfillReport:
    """What `season_cast` holds and how many shows are left to fetch."""
    totals = (await session.execute(_TOTALS)).one()
    return SeasonCreditsBackfillReport(
        shows_mirrored=totals.shows_mirrored,
        shows_stamped=totals.shows_stamped,
        shows_remaining=totals.shows_remaining,
        stamped_with_no_regulars=totals.stamped_with_no_regulars,
        season_cast_rows=(await session.execute(_SEASON_CAST_ROWS)).scalar_one(),
    )
