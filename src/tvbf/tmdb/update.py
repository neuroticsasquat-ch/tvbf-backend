"""The daily catalog delta from `/tv/changes` (NEU-1035).

`tvmaze/update.py`'s role against a feed that answers a different question. The
per-show work is identical — `mirror_series` is shared with the full pass — so
everything below is about the three ways TMDB's delta differs from TV Maze's.

## The cursor stops being an epoch and becomes a date

`/updates/shows` returned every show's last-modified epoch, so "what changed"
was a comparison and the cursor was a high-water mark. `/tv/changes` takes a
**date range** and returns the ids that changed inside it, so the cursor is the
end of the last range we covered.

That value still lives in `ingest_run.last_update_cursor`, stored as the epoch
of the date's midnight UTC, in its own lineage (`CATALOG_CURSOR_KINDS`). The
lineage scoping is what makes sharing a column safe: `get_last_successful_cursor`
has always been scoped precisely so one axis cannot resume from another's
watermark, and every reader of this lineage is in this module. A date is not
an epoch, and the encoding is a round trip rather than a reinterpretation — but
a *second* reader with TV Maze's assumptions would be a bug, which is why
`ingest.py` still writes nothing here at all.

## Windows, because a gap is not a request

TMDB caps one request at 14 days (`CHANGES_MAX_WINDOW_DAYS`) and rejects a wider
range rather than clamping it, so a container down for three weeks cannot be
caught up in one call. `plan_windows` walks the gap in consecutive windows
instead.

They share their boundary day on purpose: window *n* ends on the day window
*n+1* begins, and the same holds across runs, because a run finishing today
records today and tomorrow's run starts there. Both bounds are inclusive
upstream, so consecutive-but-disjoint windows would drop every change on the
boundary day. Overlapping costs nothing — the ids are deduplicated before
anything is fetched, and a re-fetch is idempotent anyway.

## Every hit is a full re-fetch

A changed id carries no indication of *what* changed — the response is `id` and
`adult`, nothing more. There is no cheap path, so the delta re-fetches the whole
show exactly as the full pass does, which is also what keeps `tmdb_synced_at`
meaningful afterwards.

## What this delta deliberately does not do

**It does not tombstone.** `/tv/changes` reports changes, not deletions: a
series removed from TMDB simply stops appearing, which is indistinguishable from
one that did not change. Tombstoning is a reverse diff against the *full* daily
export and is NEU-1036's, floor guards and all (ADR-0005).

**It does not filter `adult`.** The full pass mirrors whatever the export lists
and this must not disagree with it — a delta that skipped adult series would
leave the rows the export already created drifting untouched forever.
"""

import logging
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.config import Settings
from tvbf.db import SessionLocal
from tvbf.tmdb.client import (
    CHANGES_MAX_PAGE,
    CHANGES_MAX_WINDOW_DAYS,
    TMDBClient,
)
from tvbf.tmdb.ingest import (
    CatalogIngestResult,
    SessionFactory,
    _owned_session,
    mirror_series,
)
from tvbf.tvmaze.runs import (
    CATALOG_CURSOR_KINDS,
    finalize_run,
    get_completed_pass_start,
    get_last_successful_cursor,
)

log = logging.getLogger(__name__)

# The kinds a bootstrap start date can be read from when this lineage has no
# cursor yet — the full catalog pass, which writes none of its own.
_FULL_PASS_KINDS: tuple[str, ...] = ("catalog_initial",)

# How far back to look when there is neither a cursor nor a completed full pass.
# One day is TMDB's own default window for `/tv/changes`, and the honest floor:
# a delta on an unpopulated catalog has no run to bound a gap with, so anything
# larger would be a guess dressed up as a range.
_COLD_START_DAYS = 1

# A running total every N shows. Two orders of magnitude below the full pass's
# 1000 because a delta's work list is hundreds of shows rather than 229k — at
# 1000 a normal day would log nothing at all between its start and its end.
_PROGRESS_EVERY = 100


def date_to_cursor(day: date) -> int:
    """A date as the epoch of its midnight UTC — how this lineage stores it."""
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp())


def cursor_to_date(cursor: int) -> date:
    """The inverse of `date_to_cursor`. Exact, because midnight UTC round-trips."""
    return datetime.fromtimestamp(cursor, tz=UTC).date()


def plan_windows(
    start: date, end: date, *, max_days: int = CHANGES_MAX_WINDOW_DAYS
) -> list[tuple[date, date]]:
    """Split `start..end` into consecutive request-sized windows.

    Empty when there is nothing to cover — `end <= start`, including the future
    cursor a clock adjustment could leave behind, where the alternative would be
    a backwards range TMDB has no answer for.

    Each window ends where the next begins, which is deliberate: TMDB's bounds
    are inclusive, so `(d, d+14)` then `(d+15, d+29)` would silently drop every
    change dated `d+14`. The duplicate day is free — `changed_series_ids`
    deduplicates before any show is fetched.
    """
    if max_days < 1:
        raise ValueError(f"a window must span at least a day, got {max_days}")
    windows: list[tuple[date, date]] = []
    cursor = start
    while cursor < end:
        nxt = min(cursor + timedelta(days=max_days), end)
        windows.append((cursor, nxt))
        cursor = nxt
    return windows


def _series_ids_in(body: dict) -> Iterator[int]:
    """The `id`s of one `/tv/changes` page.

    A malformed entry is skipped rather than fatal, the same trade the id export
    makes: this is somebody else's feed, and losing one series to tomorrow's
    window beats losing the run.
    """
    for row in body.get("results") or []:
        series_id = row.get("id") if isinstance(row, dict) else None
        if isinstance(series_id, int) and not isinstance(series_id, bool):
            yield series_id


def split_window(start: date, end: date) -> list[tuple[date, date]] | None:
    """Halve a window, or None when it is already a single day.

    The same shared-boundary rule `plan_windows` follows, for the same reason:
    both bounds are inclusive upstream, so the halves must meet rather than
    abut.
    """
    if (end - start).days <= 1:
        return None
    mid = start + timedelta(days=(end - start).days // 2)
    return [(start, mid), (mid, end)]


async def changed_series_ids(client: TMDBClient, windows: list[tuple[date, date]]) -> list[int]:
    """Every series id that changed across `windows`, in first-seen order.

    Deduplicated across windows and pages, so the shared boundary day and a show
    that changed twice each cost one re-fetch rather than several.

    A window reporting more pages than TMDB will serve is **halved and retried**,
    not truncated. 500 pages is 50,000 changed series, which is out of reach for
    a day and squarely in reach for the 14-day windows a long gap is walked in —
    and truncating would drop the overflow silently, then let the run finalise
    `succeeded` and advance the cursor past the very days it failed to read. A
    single day over the cap has no halving left and raises: unrepresentable is a
    thing to fail on, not to paper over.
    """
    seen: dict[int, None] = {}
    pending = list(windows)
    while pending:
        start, end = pending.pop(0)
        page = 1
        while True:
            body = await client.get_tv_changes(start=start, end=end, page=page)
            total_pages = int(body.get("total_pages") or 0)
            if total_pages > CHANGES_MAX_PAGE:
                halves = split_window(start, end)
                if halves is None:
                    raise RuntimeError(
                        f"{start}..{end} reports {total_pages} pages of changes, past TMDB's "
                        f"{CHANGES_MAX_PAGE}-page cap, and a single day cannot be split further"
                    )
                log.warning(
                    "changes window %s..%s reports %d pages — halving it",
                    start,
                    end,
                    total_pages,
                )
                pending[:0] = halves
                break
            for series_id in _series_ids_in(body):
                seen[series_id] = None
            if page >= total_pages:
                log.info("changes window %s..%s: %d series so far", start, end, len(seen))
                break
            page += 1
    return list(seen)


async def resolve_start_date(session: AsyncSession, *, today: date) -> date:
    """Where this run's coverage begins.

    Three sources, first hit wins, each one narrower than the last:

    1. **This lineage's cursor** — the end of the last window a delta covered.
    2. **The completed full pass's earliest `started_at`** — the bootstrap, for
       the first delta after a catalog ingest. See `get_completed_pass_start`
       for why it is the earliest attempt's start and not the successful run's.
    3. **Yesterday**, with a warning. Nothing has ever populated this catalog, so
       there is no window to bound; a delta here is almost certainly running
       before the full pass it is supposed to follow.
    """
    cursor = await get_last_successful_cursor(session, kinds=CATALOG_CURSOR_KINDS)
    if cursor is not None:
        return cursor_to_date(cursor)

    full_pass = await get_completed_pass_start(session, kinds=_FULL_PASS_KINDS)
    if full_pass is not None:
        log.info("no delta cursor yet — bootstrapping from the full pass's start")
        return full_pass.astimezone(UTC).date()

    log.warning(
        "no delta cursor and no completed full catalog pass — covering only the last %d day(s)",
        _COLD_START_DAYS,
    )
    return today - timedelta(days=_COLD_START_DAYS)


async def run_catalog_update(
    *,
    session_factory: SessionFactory,
    client: TMDBClient,
    run_id: UUID,
    failure_threshold: int = 10,
    today: date | None = None,
) -> CatalogIngestResult:
    """One delta cycle: resolve the gap, walk it, re-fetch everything it names.

    The cursor advances only when the run itself completed. A run that aborts
    leaves it where it was, so the next run re-covers the whole gap rather than
    stepping over the part this one never reached.

    A **per-show** failure below the abort threshold is a different matter, and
    the cursor moves past it: that show stays stale until TMDB changes it again.
    Rewinding instead would re-fetch the entire window to retry one show, and a
    single permanently-broken series would then re-cover an ever-widening gap
    every night — the wedge NEU-1006 exists to avoid. `tvmaze/update.py` makes
    the same trade with `max_epoch`. The failure is counted on the run row and
    logged, so it is visible rather than silent.
    """
    today = today or datetime.now(UTC).date()

    async with _owned_session(session_factory) as s:
        start = await resolve_start_date(s, today=today)

    windows = plan_windows(start, today)
    log.info("catalog delta: %s..%s in %d window(s)", start, today, len(windows))

    series_ids = await changed_series_ids(client, windows)
    log.info("catalog delta: %d changed series to re-fetch", len(series_ids))

    result = await mirror_series(
        session_factory=session_factory,
        client=client,
        run_id=run_id,
        series_ids=series_ids,
        failure_threshold=failure_threshold,
        label="catalog delta",
        progress_every=_PROGRESS_EVERY,
    )
    if result.aborted:
        return result

    async with _owned_session(session_factory) as s:
        await finalize_run(s, run_id, status="succeeded", last_update_cursor=date_to_cursor(today))
        await s.commit()
    return result


def _session_factory():
    return SessionLocal()


async def run_catalog_update_job(run_id: UUID, settings: Settings) -> None:
    """One delta cycle, wired from settings and guaranteed to finalize.

    Two callers, as `run_update_job` has: `POST /admin/catalog-update` spawns it
    with `create_task` and the `tvbf.jobs.catalog_update` CLI awaits it. Sharing
    the body is what stops the scheduled delta and the manual trigger drifting.
    """
    try:
        async with TMDBClient(
            base_url=settings.tmdb_base_url,
            read_access_token=settings.tmdb_read_access_token,
            rate_calls=settings.tmdb_rate_limit_requests,
            rate_window=settings.tmdb_rate_limit_window_seconds,
            retry_max_attempts=settings.tmdb_retry_max_attempts,
        ) as client:
            await run_catalog_update(
                session_factory=_session_factory,
                client=client,
                run_id=run_id,
                failure_threshold=settings.ingest_consecutive_failure_threshold,
            )
    except Exception as e:
        log.exception("catalog delta crashed")
        async with SessionLocal() as s:
            await finalize_run(s, run_id, status="failed", error=str(e))
            await s.commit()
