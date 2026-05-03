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
from tvbf.tvmaze.runs import finalize_run, record_progress
from tvbf.tvmaze.schemas import TVMazeAka
from tvbf.tvmaze.upsert import mark_akas_synced, upsert_akas

log = logging.getLogger(__name__)

SessionFactory = Callable[[], AsyncSession]


@dataclass
class BackfillResult:
    shows_processed: int
    shows_failed: int


@asynccontextmanager
async def _owned_session(session_factory: SessionFactory) -> AsyncIterator[AsyncSession]:
    """Yield a session via the factory's async context manager."""
    async with session_factory() as s:
        yield s


async def run_akas_backfill(
    *,
    session_factory: SessionFactory,
    client: Any,  # duck-typed: needs `async get_akas(show_id) -> list[dict]`
    run_id: UUID,
    failure_threshold: int = 10,
) -> BackfillResult:
    """Iterate every show with akas_synced_at IS NULL; fetch + upsert AKAs.

    Each show runs in its own transaction so a crash mid-run leaves earlier
    shows synced. Per-show failures (HTTP/parse errors) bump shows_failed and
    abort the run after `failure_threshold` consecutive failures, mirroring
    the initial ingest pattern.
    """
    async with _owned_session(session_factory) as s:
        todo = (
            (
                await s.execute(
                    select(m.Show.id).where(m.Show.akas_synced_at.is_(None)).order_by(m.Show.id)
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
            payload = await client.get_akas(show_id)
        except httpx.HTTPStatusError as e:
            log.warning("akas backfill: skipping show %d after http error: %s", show_id, e)
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
            log.exception("akas backfill: unexpected error for show %d", show_id)
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
                akas = [TVMazeAka.model_validate(a) for a in payload]
                await upsert_akas(s, show_id=show_id, akas=akas)
                await mark_akas_synced(s, show_id=show_id)
                await record_progress(s, run_id, processed_delta=1)
                await s.commit()
            processed += 1
            consecutive_failures = 0
        except Exception as e:
            log.exception("akas backfill: upsert failed for show %d", show_id)
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
