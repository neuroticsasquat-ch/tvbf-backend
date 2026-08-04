from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import desc, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.tvmaze import models as m

# Run kinds that share one `last_update_cursor` lineage. Each ingest axis has
# its own: the initial ingest sets the cursor and the daily delta advances it,
# so the two kinds must be read together. Axes must NOT see each other's.
SHOW_CURSOR_KINDS: tuple[str, ...] = ("initial", "update")
PERSON_CURSOR_KINDS: tuple[str, ...] = ("person_initial", "person_update")


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


async def get_last_successful_cursor(
    session: AsyncSession, *, kinds: Sequence[str] = SHOW_CURSOR_KINDS
) -> int | None:
    """Latest `last_update_cursor` across a cursor lineage.

    Scoped to a lineage on purpose. `ingest_run.last_update_cursor` is one
    column shared by every run kind, so an unscoped query returns whichever
    run finished most recently regardless of what produced it. Once a second
    axis stores a watermark there too, each delta would resume from the
    other's position — and since every cursor is a TV Maze epoch, nothing
    errors: work is just silently skipped.

    Scoped by lineage rather than by a single kind because the initial ingest
    hands its cursor to the first daily delta (see `ingest.py`, which
    finalizes an `initial` run with `last_update_cursor`). Narrowing to
    `kind == "update"` would break that handoff and make the first delta after
    an ingest re-fetch the whole catalog.
    """
    result = await session.execute(
        select(m.IngestRun.last_update_cursor)
        .where(
            m.IngestRun.kind.in_(tuple(kinds)),
            m.IngestRun.status == "succeeded",
            m.IngestRun.last_update_cursor.is_not(None),
        )
        .order_by(desc(m.IngestRun.finished_at))
        .limit(1)
    )
    return result.scalar_one_or_none()


async def find_live_run(
    session: AsyncSession, *, kind: str, stale_after_minutes: int
) -> m.IngestRun | None:
    """The in-flight run of `kind`, or None if there isn't one.

    Liveness is `status='running'` **plus recent activity**, never status
    alone. `mark_stale_runs_cancelled` only runs in the lifespan hook, so a
    run whose process died without finalizing keeps `status='running'` until
    the container next restarts — a guard built on status alone would wedge
    that kind of job with no way out but a redeploy.

    Falls back to `started_at` when `last_progress_at` is NULL, so a run that
    has been created but has not yet recorded progress still counts as live.
    That window is precisely where an accidental double-POST lands, and it is
    also why this is stricter than `mark_stale_runs_cancelled`, which ignores
    NULL-progress rows entirely.

    Scoped to a single kind on purpose. A global check would couple every job
    to every other — a stuck backfill would block an urgent daily.
    """
    cutoff = datetime.now(UTC) - timedelta(minutes=stale_after_minutes)
    result = await session.execute(
        select(m.IngestRun)
        .where(
            m.IngestRun.kind == kind,
            m.IngestRun.status == "running",
            func.coalesce(m.IngestRun.last_progress_at, m.IngestRun.started_at) > cutoff,
        )
        .order_by(desc(m.IngestRun.started_at))
        .limit(1)
    )
    return result.scalars().first()


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
