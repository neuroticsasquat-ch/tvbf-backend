"""The full catalog ingest: every TMDB series, into `catalog` (NEU-1034).

The `tvmaze/ingest.py` role, and the operational shape ports wholesale — a
resumable work list, per-show failures that are counted rather than fatal, an
abort after N consecutive failures, and a run row carrying progress. What is
new is entirely a consequence of the two sources being shaped differently.

## The work list is a file, and "already done" is not "already present"

TV Maze's `/updates/shows` was an endpoint; TMDB's equivalent is a static daily
file (see `export.py`). The diff against it is where the resemblance stops.

`tvmaze/ingest.py` resumes by diffing the feed against **rows that exist**,
which works because a `tvmaze.show` row exists only if that ingest created it.
Here it would be wrong twice over: NEU-1042 copied ~89k TV Maze shows into
`catalog`, and NEU-1043 attached a `tmdb_id` to ~63k of them. Those rows exist,
carry the right `tmdb_id`, and hold **TV Maze data** — an ingest that skipped
them would finish having never fetched the shows users actually track.

So the watermark is `catalog.show.tmdb_synced_at`, set only once a full payload
has been mirrored. It is the same device `tvmaze.show.akas_synced_at` and
`tvmaze.season.credits_synced_at` already are, and the reason a killed run
resumes rather than restarts.

## One request per show, and the guess that makes it one

`append_to_response` takes 20 entries, namespaces and `season/N` blocks drawing
on the same budget (NEU-1028). The audit's 11 namespaces leave nine season
slots — but a show's season *numbers* are only knowable from a response we have
not made yet, so the first request guesses a window and reconciles afterwards
against `seasons[]`, fetching whatever it missed with `get_tv_season`.

Both halves of that guess are measured, by `scripts/probe_tmdb_season_speculation.py`
against the live API on 2026-08-10:

- **Guessing is free.** A `season/N` a show does not have is dropped from the
  response silently — 200 OK, key simply absent. Verified on a 3-season show
  asked for six absent seasons. Had TMDB 400'd instead, speculation would have
  broken the ingest for the single-season series that are most of the catalog.
- **`0..8` beats `1..9`.** Across 200 sampled series, 97.5% have every season
  inside `0..8` against 94.0% inside `1..9`. Specials (season 0) are rarer than
  expected at 3.5%, but shows with a ninth numbered season are rarer still.

Correctness does not rest on the guess — the reconcile step covers whatever it
missed. The guess only decides how many shows cost two requests instead of one.

## How long it takes: ~8.7 hours, and the budget is not why

**Measured 2026-08-10 over a 150-show sample spread through the export: 7.27
shows/sec, so ~8.7 hours for the full ~229k.** The project spec's ~3.2-hour
figure was derived from the request budget and does not survive contact — this
is the same correction NEU-1065 had to make to the enrichment pass, for the same
reason. The loop is **sequential**, so throughput is set by round-trip latency;
at ~1.05 requests per show the pass draws about 7.6 req/s against a 20 req/s
allowance, and the budget is never the binding constraint. Widening the append
list or the season window would therefore buy less than it looks like it should,
and concurrency is the only lever that would matter.

The pass is resumable and per-show idempotent, so it is safe to kill and restart
across that window rather than holding one process open for it.

## What this ingest deliberately does not do

**It writes no `last_update_cursor`.** TV Maze's cursor was a per-show epoch the
initial ingest handed to the first daily delta. TMDB's delta is `/tv/changes`
over a date *range* (NEU-1035), so there is no epoch to hand over, and writing
one into a column typed for TV Maze's would be a value the next reader
misreads.

**It does not touch the copied TV Maze seasons and episodes.** A show that was
copied and then enriched carries both its TV Maze rows (`tmdb_id IS NULL`) and
the TMDB rows this ingest writes. `prune_missing_seasons` steps around the
former by design — `app.user_episode_watch` points at those preserved ids, so
deleting them would destroy watch history nothing upstream could restore.
Reconciling the two grains is NEU-1045's (episodes) and NEU-1066's (shows).
"""

import logging
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.catalog import models as m
from tvbf.tmdb.api_payloads import TMDBSeasonDetail, TMDBSeries
from tvbf.tmdb.client import (
    APPEND_TO_RESPONSE_LIMIT,
    DEFAULT_APPEND,
    TMDBClient,
    is_gone_upstream,
    plan_append,
)
from tvbf.tmdb.export import fetch_series_ids
from tvbf.tmdb.upsert import mark_series_synced, upsert_series_payload
from tvbf.tvmaze.runs import finalize_run, record_progress, warn_if_all_gone

log = logging.getLogger(__name__)

# The season numbers the first request guesses at, sized to whatever
# `append_to_response` has left after the audit's namespaces. Derived rather
# than written out so adding a twelfth namespace narrows the window instead of
# silently overflowing the cap.
#
# Starts at 0 — specials — because the measurement says so: 0..8 covers 97.5% of
# sampled shows against 94.0% for 1..9. See the module docstring.
SPECULATIVE_SEASONS: tuple[int, ...] = tuple(
    range(0, APPEND_TO_RESPONSE_LIMIT - len(DEFAULT_APPEND))
)


# How often to log a running total. At the measured 7.27 shows/sec this is a
# line every ~2 minutes over an 8.7-hour pass — frequent enough to tell "slow"
# from "wedged", sparse enough not to bury the per-show warnings.
_PROGRESS_EVERY = 1000


@dataclass
class CatalogIngestResult:
    shows_processed: int
    shows_failed: int
    shows_gone: int


def _log_progress(processed: int, failed: int, gone: int, total: int) -> None:
    """A running total, with the two kinds of failure kept apart.

    `ingest_run.shows_failed` is one column and counts both, so an operator
    polling the run row over ~229k ids sees a failure count they cannot read: a
    thousand series TMDB has deleted looks exactly like a thousand broken
    requests. Splitting `gone` out here is what makes the difference legible
    without a schema change, and the denominator is what makes a bare
    `shows_processed` mean something.
    """
    log.info(
        "catalog ingest: %d/%d processed, %d failed (%d gone upstream, %d real)",
        processed,
        total,
        failed,
        gone,
        failed - gone,
    )


SessionFactory = Callable[[], AsyncSession]


@asynccontextmanager
async def _owned_session(session_factory: SessionFactory) -> AsyncIterator[AsyncSession]:
    """Yield a session via the factory's async context manager."""
    async with session_factory() as s:
        yield s


async def synced_series_ids(session: AsyncSession) -> set[int]:
    """Every `tmdb_id` a full payload has already been mirrored for.

    Read in one query and diffed in Python rather than pushed into the work
    list as `tmdb_id NOT IN (:ids)`: the export is ~229k ids against Postgres's
    32,767 bind-parameter cap — the same ceiling that forces the episode batch
    size and the tombstone reconciler's Python-side diff.
    """
    rows = await session.execute(
        select(m.Show.tmdb_id).where(
            m.Show.tmdb_id.is_not(None), m.Show.tmdb_synced_at.is_not(None)
        )
    )
    return {tmdb_id for tmdb_id in rows.scalars().all() if tmdb_id is not None}


async def fetch_series_with_seasons(
    client: TMDBClient, series_id: int
) -> tuple[TMDBSeries, list[TMDBSeasonDetail]]:
    """One show, complete: the series request plus however many seasons overflowed.

    Returns the parsed series and the season details that did **not** ride the
    first request. `upsert_series_payload` merges the two by season number.

    An overflow fetch that fails takes the whole show down with it, and that is
    the point: a show written with one season's episodes missing would be
    stamped `tmdb_synced_at` and never revisited, which is exactly the silent
    partial the watermark exists to prevent. Failing leaves the watermark null,
    so the next run picks the show up again.
    """
    append, _ = plan_append(SPECULATIVE_SEASONS)
    series = TMDBSeries.model_validate(await client.get_tv_series(series_id, append=append))

    arrived = {detail.season_number for detail in series.appended_seasons}
    missing = sorted({summary.season_number for summary in series.seasons} - arrived)
    overflow = [
        TMDBSeasonDetail.model_validate(await client.get_tv_season(series_id, number))
        for number in missing
    ]
    if overflow:
        log.debug("series %d: %d season(s) needed a follow-up request", series_id, len(overflow))
    return series, overflow


async def run_catalog_ingest(
    *,
    session_factory: SessionFactory,
    client: TMDBClient,
    run_id: UUID,
    failure_threshold: int = 10,
    series_ids: Sequence[int] | None = None,
) -> CatalogIngestResult:
    """Mirror every series in the daily id export into `catalog`.

    `series_ids` overrides the export download, which is what tests and a
    targeted re-run use; leaving it unset fetches the real thing.
    """
    export_ids = list(series_ids) if series_ids is not None else await fetch_series_ids()

    async with _owned_session(session_factory) as s:
        synced = await synced_series_ids(s)
    # `dict.fromkeys` rather than a set, so the work list keeps the export's own
    # order and a resumed run picks up roughly where the last one stopped.
    todo = [series_id for series_id in dict.fromkeys(export_ids) if series_id not in synced]
    log.info("catalog ingest: %d series in the export, %d to fetch", len(export_ids), len(todo))

    processed = 0
    failed = 0
    gone = 0
    consecutive_failures = 0

    for series_id in todo:
        try:
            series, overflow = await fetch_series_with_seasons(client, series_id)
            async with _owned_session(session_factory) as s:
                show_id = await upsert_series_payload(
                    s,
                    series,
                    seasons=overflow,
                    # The series body carries the authoritative `seasons[]`
                    # whatever else was appended, so the payload can be trusted
                    # to name the show's whole season set (ADR-0004).
                    prune_seasons=True,
                )
                await mark_series_synced(s, show_id=show_id)
                await record_progress(s, run_id, processed_delta=1)
                await s.commit()
        except Exception as exc:
            failed += 1
            if is_gone_upstream(exc):
                # An id the export lists and `/tv/{id}` no longer serves. A data
                # condition, not a broken upstream, so it must not count toward
                # the abort (NEU-1006).
                gone += 1
                log.info("series %d is gone upstream — skipping", series_id)
            else:
                consecutive_failures += 1
                if isinstance(exc, httpx.HTTPStatusError):
                    log.warning("skipping series %d after http error: %s", series_id, exc)
                else:
                    log.exception("unexpected error for series %d", series_id)
            async with _owned_session(session_factory) as s:
                await record_progress(s, run_id, failed_delta=1)
                await s.commit()
            if consecutive_failures >= failure_threshold:
                async with _owned_session(session_factory) as s:
                    await finalize_run(
                        s,
                        run_id,
                        status="failed",
                        error=(f"aborted after {consecutive_failures} consecutive failures: {exc}"),
                    )
                    await s.commit()
                return CatalogIngestResult(processed, failed, gone)
            continue

        processed += 1
        consecutive_failures = 0
        if processed % _PROGRESS_EVERY == 0:
            _log_progress(processed, failed, gone, len(todo))

    _log_progress(processed, failed, gone, len(todo))
    warn_if_all_gone(log, processed=processed, failed=failed, gone=gone, noun="series")
    async with _owned_session(session_factory) as s:
        # No `last_update_cursor`: TMDB's delta is a date range rather than a
        # per-show epoch, so there is nothing to hand forward. See the module
        # docstring.
        await finalize_run(s, run_id, status="succeeded")
        await s.commit()

    return CatalogIngestResult(processed, failed, gone)
