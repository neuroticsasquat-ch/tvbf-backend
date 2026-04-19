import logging
from uuid import UUID

import httpx

from tvbf.tvmaze.client import TVMazeClient
from tvbf.tvmaze.ingest import IngestResult, SessionFactory, _owned_session
from tvbf.tvmaze.runs import finalize_run, get_last_successful_cursor, record_progress
from tvbf.tvmaze.schemas import TVMazeShow
from tvbf.tvmaze.upsert import upsert_show_payload

log = logging.getLogger(__name__)


async def run_update(
    *,
    session_factory: SessionFactory,
    client: TVMazeClient,
    run_id: UUID,
    failure_threshold: int = 10,
) -> IngestResult:
    async with _owned_session(session_factory) as s:
        cursor = await get_last_successful_cursor(s) or 0

    updates = await client.get_show_updates()
    todo = sorted(sid for sid, epoch in updates.items() if epoch > cursor)
    max_epoch = max((updates[sid] for sid in todo), default=cursor)

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

        try:
            async with _owned_session(session_factory) as s:
                await upsert_show_payload(s, TVMazeShow.model_validate(payload))
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
        await finalize_run(s, run_id, status="succeeded", last_update_cursor=max_epoch)
        await s.commit()

    return IngestResult(processed, failed, max_epoch)
