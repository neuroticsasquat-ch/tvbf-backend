from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import desc, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.tvmaze import models as m


async def create_run(session: AsyncSession, kind: str) -> UUID:
    run = m.IngestRun(id=uuid4(), kind=kind, status="running")
    session.add(run)
    await session.flush()
    return run.id


async def record_progress(
    session: AsyncSession, run_id: UUID, processed_delta: int = 0, failed_delta: int = 0
) -> None:
    now = datetime.now(UTC)
    await session.execute(
        update(m.IngestRun)
        .where(m.IngestRun.id == run_id)
        .values(
            shows_processed=m.IngestRun.shows_processed + processed_delta,
            shows_failed=m.IngestRun.shows_failed + failed_delta,
            last_progress_at=now,
        )
    )


async def finalize_run(
    session: AsyncSession,
    run_id: UUID,
    *,
    status: str,
    last_update_cursor: int | None = None,
    error: str | None = None,
) -> None:
    now = datetime.now(UTC)
    values = {"status": status, "finished_at": now}
    if last_update_cursor is not None:
        values["last_update_cursor"] = last_update_cursor
    if error is not None:
        values["error"] = error
    await session.execute(update(m.IngestRun).where(m.IngestRun.id == run_id).values(**values))


async def get_last_successful_cursor(session: AsyncSession) -> int | None:
    result = await session.execute(
        select(m.IngestRun.last_update_cursor)
        .where(m.IngestRun.status == "succeeded", m.IngestRun.last_update_cursor.is_not(None))
        .order_by(desc(m.IngestRun.finished_at))
        .limit(1)
    )
    return result.scalar_one_or_none()


async def mark_stale_runs_cancelled(session: AsyncSession, *, stale_after_minutes: int) -> int:
    cutoff = datetime.now(UTC) - timedelta(minutes=stale_after_minutes)
    result = await session.execute(
        update(m.IngestRun)
        .where(
            m.IngestRun.status == "running",
            m.IngestRun.last_progress_at.is_not(None),
            m.IngestRun.last_progress_at < cutoff,
        )
        .values(
            status="cancelled",
            finished_at=datetime.now(UTC),
            error="cancelled by startup cleanup (no progress beyond staleness threshold)",
        )
    )
    return result.rowcount or 0  # type: ignore[attr-defined]  # CursorResult has rowcount
