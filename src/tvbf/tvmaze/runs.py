import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import desc, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.tvmaze import models as m

# Run kinds that share one `last_update_cursor` lineage. Each ingest axis has
# its own: the initial ingest sets the cursor and the daily delta advances it,
# so the two kinds must be read together. Axes must NOT see each other's.
#
# The person lineage is a single kind since NEU-962 retired the initial pass,
# and stays a named lineage rather than an inlined literal because the scoping
# is the point: unscoped, the person delta would resume from the show axis's
# cursor and silently skip work (see `get_last_successful_cursor`).
SHOW_CURSOR_KINDS: tuple[str, ...] = ("initial", "update")
PERSON_CURSOR_KINDS: tuple[str, ...] = ("person_update",)

# The TMDB catalog delta's lineage (NEU-1035). A single kind, because the full
# catalog pass deliberately hands nothing forward: TMDB's delta is a date range
# rather than a per-show epoch, so `catalog_initial` has no cursor to write and
# `tmdb/update.py` bootstraps off that run's `started_at` instead.
#
# The scoping is what makes it safe for this lineage to store a *date* in a
# column every other lineage stores a TV Maze epoch in. Nothing outside these
# kinds can read it, and nothing inside them reads anything else.
CATALOG_CURSOR_KINDS: tuple[str, ...] = ("catalog_update",)


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


async def get_completed_pass_start(
    session: AsyncSession, *, kinds: Sequence[str]
) -> datetime | None:
    """When a completed pass of `kinds` first began, or None if none completed.

    Its one caller is the catalog delta bootstrapping off the full pass
    (NEU-1035), and both halves of the question are load-bearing.

    **A run must have succeeded**, because that is the only evidence the pass
    covered the whole catalog; anything less and the delta would claim coverage
    a half-finished pass never achieved.

    **The date comes from the earliest attempt, not that successful run.** The
    full pass is resumable and takes ~8.7 hours, so it is routinely several runs
    of which only the last reaches `succeeded` — and a show mirrored by the
    *first* attempt was stamped days before the last one started. Bootstrapping
    from the successful run's `started_at` would step over every change made to
    those shows in between, and nothing would ever pick them up again: they
    carry `tmdb_synced_at`, so the full pass's own work list excludes them too.
    `started_at` rather than `finished_at` for the same reason one scale down.
    """
    completed = await session.execute(
        select(func.count())
        .select_from(m.IngestRun)
        .where(m.IngestRun.kind.in_(tuple(kinds)), m.IngestRun.status == "succeeded")
    )
    if not completed.scalar_one():
        return None
    earliest = await session.execute(
        select(func.min(m.IngestRun.started_at)).where(m.IngestRun.kind.in_(tuple(kinds)))
    )
    return earliest.scalar_one_or_none()


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


def warn_if_all_gone(
    log: logging.Logger, *, processed: int, failed: int, gone: int, noun: str
) -> None:
    """Log when a run achieved nothing and every failure was a deleted entity.

    Not a failure condition: a work list that happens to be entirely deleted
    entities is a normal small day, and failing on it would reintroduce exactly
    the wedge NEU-1006 removes. But it is also what a stale work list looks
    like, so it is worth saying out loud.

    `gone == failed` is load-bearing. Without it, a run with nine persistent
    5xx and one 404 would report "all 1 entities were gone upstream" — false,
    and most misleading precisely during a partial outage, which is when
    someone is reading the logs.
    """
    if processed == 0 and gone and gone == failed:
        log.warning(
            "all %d attempted %s were gone upstream and none succeeded; the work list may be stale",
            gone,
            noun,
        )
