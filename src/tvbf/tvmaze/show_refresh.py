import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.tvmaze import models as m
from tvbf.tvmaze.api_payloads import TVMazeEpisode, TVMazeShow
from tvbf.tvmaze.runs import finalize_run, record_progress
from tvbf.tvmaze.upsert import (
    mark_credits_synced,
    mark_ratings_synced,
    upsert_show_cast,
    upsert_show_crew,
    upsert_show_payload,
)

log = logging.getLogger(__name__)

SessionFactory = Callable[[], AsyncSession]

# `episodes` is deliberately absent. /shows/{id}/episodes?specials=1 returns the
# full episode list including specials, so embedding episodes would ship a
# redundant copy of it on every one of ~87k requests.
_REFRESH_EMBEDS = ["seasons", "cast", "crew"]


@dataclass
class BackfillResult:
    shows_processed: int
    shows_failed: int


@asynccontextmanager
async def _owned_session(session_factory: SessionFactory) -> AsyncIterator[AsyncSession]:
    """Yield a session via the factory's async context manager."""
    async with session_factory() as s:
        yield s


async def run_show_refresh(
    *,
    session_factory: SessionFactory,
    client: Any,  # duck-typed: needs `get_show(id, *, embed=...)` + `get_show_episodes(id)`
    run_id: UUID,
    failure_threshold: int = 10,
) -> BackfillResult:
    """Pass A: re-fetch every show for cast, crew, externals, ratings and specials.

    Iterates every show with `credits_synced_at IS NULL`, two requests each:

    1. `/shows/{id}?embed[]=seasons&embed[]=cast&embed[]=crew`
    2. `/shows/{id}/episodes?specials=1`

    Cast and crew are the point; `externals_tvdb` (NEU-922), `rating.average`
    (NEU-161) and specials (NEU-933) ride along because this pass re-fetches
    every show anyway and each would otherwise cost its own 13.5h of the shared
    rate-limit budget.

    Each show runs in its own transaction so a crash mid-run leaves earlier
    shows synced. Per-show failures bump `shows_failed` and abort the run after
    `failure_threshold` consecutive failures, mirroring the AKAs backfill.

    Unlike the ongoing ingest paths, a failed episodes fetch fails the *show*
    rather than falling back — the watermark is what makes this pass resumable,
    so stamping it on a show whose specials never arrived would strand them
    until someone spent another 27 hours.
    """
    async with _owned_session(session_factory) as s:
        todo = (
            (
                await s.execute(
                    select(m.Show.id).where(m.Show.credits_synced_at.is_(None)).order_by(m.Show.id)
                )
            )
            .scalars()
            .all()
        )

    processed = 0
    failed = 0
    consecutive_failures = 0

    for show_id in todo:
        try:
            payload = await client.get_show(show_id, embed=_REFRESH_EMBEDS)
            episodes_payload = await client.get_show_episodes(show_id, specials=True)
        except httpx.HTTPStatusError as e:
            log.warning("show refresh: skipping show %d after http error: %s", show_id, e)
            failed += 1
            consecutive_failures += 1
            async with _owned_session(session_factory) as s:
                await record_progress(s, run_id, failed_delta=1)
                await s.commit()
            if consecutive_failures >= failure_threshold:
                async with _owned_session(session_factory) as s:
                    await finalize_run(
                        s,
                        run_id,
                        status="failed",
                        error=(f"aborted after {consecutive_failures} consecutive failures"),
                    )
                    await s.commit()
                return BackfillResult(processed, failed)
            continue
        except Exception as e:
            log.exception("show refresh: unexpected error for show %d", show_id)
            failed += 1
            consecutive_failures += 1
            async with _owned_session(session_factory) as s:
                await record_progress(s, run_id, failed_delta=1)
                await s.commit()
            if consecutive_failures >= failure_threshold:
                async with _owned_session(session_factory) as s:
                    await finalize_run(
                        s,
                        run_id,
                        status="failed",
                        error=(f"aborted after {consecutive_failures} consecutive failures: {e}"),
                    )
                    await s.commit()
                return BackfillResult(processed, failed)
            continue

        try:
            async with _owned_session(session_factory) as s:
                show = TVMazeShow.model_validate(payload)
                episodes = [TVMazeEpisode.model_validate(e) for e in episodes_payload]
                await upsert_show_payload(s, show, episodes=episodes)
                await upsert_show_cast(s, show_id=show.id, entries=show.embedded.cast)
                await upsert_show_crew(s, show_id=show.id, entries=show.embedded.crew)
                await mark_credits_synced(s, show_id=show.id)
                # NEU-161's ratings backfill is believed complete in prod, but
                # stamping here is free and makes a re-run a no-op either way.
                await mark_ratings_synced(s, show_id=show.id)
                await record_progress(s, run_id, processed_delta=1)
                await s.commit()
            processed += 1
            consecutive_failures = 0
        except Exception as e:
            log.exception("show refresh: write failed for show %d", show_id)
            failed += 1
            consecutive_failures += 1
            async with _owned_session(session_factory) as s:
                await record_progress(s, run_id, failed_delta=1)
                await s.commit()
            if consecutive_failures >= failure_threshold:
                async with _owned_session(session_factory) as s:
                    await finalize_run(
                        s,
                        run_id,
                        status="failed",
                        error=(f"aborted after {consecutive_failures} consecutive failures: {e}"),
                    )
                    await s.commit()
                return BackfillResult(processed, failed)

    async with _owned_session(session_factory) as s:
        await finalize_run(s, run_id, status="succeeded")
        await s.commit()

    return BackfillResult(processed, failed)
