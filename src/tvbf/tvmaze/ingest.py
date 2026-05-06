import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.tvmaze import models as m
from tvbf.tvmaze.api_payloads import TVMazeAka, TVMazeShow
from tvbf.tvmaze.client import TVMazeClient
from tvbf.tvmaze.runs import finalize_run, record_progress
from tvbf.tvmaze.upsert import mark_akas_synced, upsert_akas, upsert_show_payload

log = logging.getLogger(__name__)


@dataclass
class IngestResult:
    shows_processed: int
    shows_failed: int
    last_update_cursor: int | None


SessionFactory = Callable[[], AsyncSession]


@asynccontextmanager
async def _owned_session(session_factory: SessionFactory) -> AsyncIterator[AsyncSession]:
    """Yield a session via the factory's async context manager."""
    async with session_factory() as s:
        yield s


async def run_initial_ingest(
    *,
    session_factory: SessionFactory,
    client: TVMazeClient,
    run_id: UUID,
    failure_threshold: int = 10,
) -> IngestResult:
    async with _owned_session(session_factory) as s:
        updates = await client.get_show_updates()
        cursor = max(updates.values()) if updates else None
        existing = set((await s.execute(select(m.Show.id))).scalars().all())
        todo = sorted(set(updates) - existing)

    processed = 0
    failed = 0
    consecutive_failures = 0

    for show_id in todo:
        try:
            payload = await client.get_show(show_id)
        except httpx.HTTPStatusError as e:
            log.warning("skipping show %d after http error: %s", show_id, e)
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
                        error=f"aborted after {consecutive_failures} consecutive failures",
                    )
                    await s.commit()
                return IngestResult(processed, failed, cursor)
            continue
        except Exception as e:
            log.exception("unexpected error for show %d", show_id)
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
                        error=f"aborted after {consecutive_failures} consecutive failures: {e}",
                    )
                    await s.commit()
                return IngestResult(processed, failed, cursor)
            continue

        try:
            akas_payload = await client.get_akas(show_id)
        except Exception as e:
            log.warning(
                "akas fetch failed for show %d; will retry via backfill: %s",
                show_id,
                e,
            )
            akas_payload = None

        try:
            async with _owned_session(session_factory) as s:
                show = TVMazeShow.model_validate(payload)
                await upsert_show_payload(s, show)
                if akas_payload is not None:
                    akas = [TVMazeAka.model_validate(a) for a in akas_payload]
                    await upsert_akas(s, show_id=show.id, akas=akas)
                    await mark_akas_synced(s, show_id=show.id)
                await record_progress(s, run_id, processed_delta=1)
                await s.commit()
            processed += 1
            consecutive_failures = 0
        except Exception as e:
            log.exception("upsert failed for show %d", show_id)
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
                        error=f"aborted after {consecutive_failures} consecutive failures: {e}",
                    )
                    await s.commit()
                return IngestResult(processed, failed, cursor)

    async with _owned_session(session_factory) as s:
        await finalize_run(s, run_id, status="succeeded", last_update_cursor=cursor)
        await s.commit()

    return IngestResult(processed, failed, cursor)
