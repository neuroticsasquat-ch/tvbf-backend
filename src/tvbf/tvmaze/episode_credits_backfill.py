import logging
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import groupby
from typing import Any
from uuid import UUID

from sqlalchemy import select

from tvbf.tvmaze import models as m
from tvbf.tvmaze.client import is_gone_upstream
from tvbf.tvmaze.ingest import SessionFactory, _owned_session
from tvbf.tvmaze.runs import finalize_run, record_progress, warn_if_all_gone
from tvbf.tvmaze.season_credits import write_season_credits

log = logging.getLogger(__name__)


@dataclass
class BackfillResult:
    seasons_processed: int
    seasons_failed: int


async def _load_todo(session_factory: SessionFactory) -> Sequence[tuple[int, int]]:
    """Every unstamped season as `(show_id, season_id)`, grouped by show.

    Ordered by `(show_id, number, id)` rather than by `season.id`, which is what
    every other pass uses. Season ids are not contiguous per show: 12,026 shows
    have season ids spanning more than 20,000, which across 188k seasons at
    ~29 hours is over three hours apart. Under id ordering roughly 13.5% of the
    catalogue — disproportionately the long-running shows people actually track
    — would render a visibly partial crew list for hours mid-run. Under show
    ordering at most one show is ever half-populated.

    Nothing is lost by reordering: the watermark is the only run state, so
    resumability does not depend on the order and the order is free to optimise
    for what a user sees mid-run.
    """
    async with _owned_session(session_factory) as s:
        rows = (
            await s.execute(
                # Tombstoned shows are excluded: their seasons can never be
                # stamped, because /seasons/{id}/episodes 404s once the parent
                # show is gone. Left in, they would sit in this work list
                # forever, re-fetched and re-failed by every run — the exact
                # condition NEU-967 needed 58 hand-deletions to clear (ADR-0005).
                select(m.Season.show_id, m.Season.id)
                .join(m.Show, m.Show.id == m.Season.show_id)
                .where(
                    m.Season.credits_synced_at.is_(None),
                    m.Show.deleted_upstream_at.is_(None),
                )
                .order_by(m.Season.show_id, m.Season.number, m.Season.id)
            )
        ).all()
    return [(show_id, season_id) for show_id, season_id in rows]


async def run_episode_credits_backfill(
    *,
    session_factory: SessionFactory,
    client: Any,  # duck-typed: needs `async get_season_episodes(season_id) -> list[dict]`
    run_id: UUID,
    failure_threshold: int = 10,
) -> BackfillResult:
    """Fetch episode credits for every season with `credits_synced_at IS NULL`.

    188,189 seasons at 18 req/10s ≈ 29 hours (ADR-0003). Each season is one
    request and one transaction, so a crash mid-run costs at most the in-flight
    season and a re-trigger resumes from the watermark — there is no offset to
    lose.

    Progress is counted in `shows_processed` / `shows_failed`, which here count
    *seasons*. Every prior pass already repurposes those columns (the person
    passes counted people); renaming them would be a migration for cosmetics.

    Writes no `last_update_cursor`, so it cannot pollute the show lineage —
    `get_last_successful_cursor` filters on `last_update_cursor IS NOT NULL`,
    and this kind is deliberately absent from `SHOW_CURSOR_KINDS` besides.

    Per-season failures are non-fatal and counted. **Consecutive failures are
    counted per show, not per season**: the counter resets at every show
    boundary and the run aborts only after `failure_threshold` consecutive
    shows fail wholesale. Show-grouped ordering concentrates what used to be
    diffuse — 2,560 shows have 10 or more seasons, so under a per-season rule a
    single show removed or broken upstream produces enough consecutive failures
    to abort a run twenty hours in. The threshold is meant to say "upstream is
    broken, stop", not "one long-running show is gone".

    "Wholesale" means every season of the show *in this run's todo list*, which
    on a resumed run is only the seasons still unstamped. That is the reading
    the threshold wants: if upstream is broken every attempt fails regardless of
    how many seasons a show has left, and if it is healthy no run of shows fails
    every remaining season in a row. Counting against a show's full season list
    would cost a query per show to protect against a case that cannot arise.
    """
    todo = await _load_todo(session_factory)

    processed = 0
    failed = 0
    consecutive_failed_shows = 0
    # Season-grained, deliberately: `failed` counts seasons, and warn_if_all_gone
    # compares the two. A show-grained counter here would be comparing shows to
    # seasons and the warning would essentially never fire.
    gone_seasons = 0

    for show_id, group in groupby(todo, key=lambda row: row[0]):
        show_succeeded = False
        # True while every failure on this show has been a 404. That means the
        # seasons are gone — the show itself may be deleted, or these may be
        # phantom seasons on a live show, which is what NEU-961's 245 season
        # 404s actually were. Either way it is a data condition rather than an
        # outage, which is all this flag needs to decide (NEU-1006).
        show_only_gone = True
        for _, season_id in group:
            try:
                await write_season_credits(
                    client=client, session_factory=session_factory, season_id=season_id
                )
            except Exception as e:
                log.exception(
                    "episode credits backfill: season %d of show %d failed", season_id, show_id
                )
                failed += 1
                if is_gone_upstream(e):
                    gone_seasons += 1
                else:
                    show_only_gone = False
                async with _owned_session(session_factory) as s:
                    await record_progress(s, run_id, failed_delta=1)
                    await s.commit()
                continue

            processed += 1
            show_succeeded = True
            async with _owned_session(session_factory) as s:
                await record_progress(s, run_id, processed_delta=1)
                await s.commit()

        if show_succeeded:
            consecutive_failed_shows = 0
            continue

        if show_only_gone:
            # Every season of this show 404d, so nothing here is evidence that
            # upstream is broken. Leave the counter untouched rather than reset
            # it, so a real outage interleaved with gone seasons still
            # accumulates toward the abort.
            continue

        consecutive_failed_shows += 1
        if consecutive_failed_shows >= failure_threshold:
            async with _owned_session(session_factory) as s:
                await finalize_run(
                    s,
                    run_id,
                    status="failed",
                    error=(
                        f"aborted after {consecutive_failed_shows} consecutive shows "
                        f"whose every season failed (last: show {show_id})"
                    ),
                )
                await s.commit()
            return BackfillResult(processed, failed)

    async with _owned_session(session_factory) as s:
        warn_if_all_gone(log, processed=processed, failed=failed, gone=gone_seasons, noun="seasons")
        await finalize_run(s, run_id, status="succeeded")
        await s.commit()

    return BackfillResult(processed, failed)
