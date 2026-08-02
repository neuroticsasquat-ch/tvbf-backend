from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select

from tvbf.tvmaze import models as m
from tvbf.tvmaze.runs import (
    SHOW_CURSOR_KINDS,
    create_run,
    finalize_run,
    get_last_successful_cursor,
    mark_stale_runs_cancelled,
    record_progress,
)


async def test_create_run_inserts_with_running_status(session):
    run_id = await create_run(session, kind="initial")
    await session.commit()
    assert isinstance(run_id, UUID)
    row = (await session.execute(select(m.IngestRun).where(m.IngestRun.id == run_id))).scalar_one()
    assert row.kind == "initial"
    assert row.status == "running"
    assert row.shows_processed == 0


async def test_record_progress_increments_counters_and_stamps(session):
    run_id = await create_run(session, kind="update")
    await session.commit()
    await record_progress(session, run_id, processed_delta=2, failed_delta=1)
    await session.commit()
    row = (await session.execute(select(m.IngestRun).where(m.IngestRun.id == run_id))).scalar_one()
    assert row.shows_processed == 2
    assert row.shows_failed == 1
    assert row.last_progress_at is not None


async def test_finalize_run_sets_status_and_cursor(session):
    run_id = await create_run(session, kind="initial")
    await session.commit()
    await finalize_run(session, run_id, status="succeeded", last_update_cursor=42)
    await session.commit()
    row = (await session.execute(select(m.IngestRun).where(m.IngestRun.id == run_id))).scalar_one()
    assert row.status == "succeeded"
    assert row.last_update_cursor == 42
    assert row.finished_at is not None


async def test_get_last_successful_cursor_returns_latest(session):
    # Both runs are on the show axis, so this exercises recency ordering
    # rather than axis filtering — see ..._is_scoped_to_its_axis.
    r1 = await create_run(session, kind="initial")
    r2 = await create_run(session, kind="update")
    await session.commit()
    await finalize_run(session, r1, status="succeeded", last_update_cursor=10)
    await finalize_run(session, r2, status="succeeded", last_update_cursor=20)
    await session.commit()
    await _pin_finished_at(
        session,
        (r1, datetime(2026, 1, 1, tzinfo=UTC)),
        (r2, datetime(2026, 6, 1, tzinfo=UTC)),
    )

    assert await get_last_successful_cursor(session) == 20


async def test_get_last_successful_cursor_none_when_no_runs(session):
    assert await get_last_successful_cursor(session) is None


async def _pin_finished_at(session, *pairs) -> None:
    """Pin finished_at so recency ordering is deterministic, not clock-dependent."""
    for run_id, finished in pairs:
        row = (
            await session.execute(select(m.IngestRun).where(m.IngestRun.id == run_id))
        ).scalar_one()
        row.finished_at = finished
    await session.commit()


async def test_initial_ingest_cursor_is_inherited_by_the_daily_delta(session):
    """The initial ingest hands its cursor to the first delta.

    `ingest.py` finalizes a succeeded `initial` run with `last_update_cursor`,
    and the first `update` run has no predecessor of its own to read. Scoping
    the lookup to `kind == "update"` would break this handoff and make that
    first delta fall back to 0 — re-fetching the entire ~87k-show catalog.
    """
    initial = await create_run(session, kind="initial")
    await session.commit()
    await finalize_run(session, initial, status="succeeded", last_update_cursor=100)
    await session.commit()

    assert await get_last_successful_cursor(session) == 100


async def test_get_last_successful_cursor_is_scoped_to_its_axis(session):
    """A newer run on another axis must not shadow this axis's watermark.

    `ingest_run.last_update_cursor` is one column shared by every run kind.
    Unscoped, each delta reads whichever run finished most recently — so the
    two axes resume from each other's position. Both cursors are TV Maze epoch
    seconds, so nothing errors; work is just silently skipped.

    Uses `akas_backfill` as the stand-in off-axis kind because it is already
    in the `ck_ingest_run_kind` whitelist. The real second cursor lineage
    (`person_initial` / `person_update`) arrives with the Cast and Crew tables.
    """
    show_run = await create_run(session, kind="update")
    other_axis = await create_run(session, kind="akas_backfill")
    await session.commit()
    await finalize_run(session, show_run, status="succeeded", last_update_cursor=100)
    await finalize_run(session, other_axis, status="succeeded", last_update_cursor=999)
    await session.commit()
    await _pin_finished_at(
        session,
        (show_run, datetime(2026, 1, 1, tzinfo=UTC)),
        (other_axis, datetime(2026, 6, 1, tzinfo=UTC)),
    )

    assert await get_last_successful_cursor(session, kinds=SHOW_CURSOR_KINDS) == 100
    assert await get_last_successful_cursor(session, kinds=("akas_backfill",)) == 999


async def test_get_last_successful_cursor_none_when_only_other_axes_have_one(session):
    run_id = await create_run(session, kind="akas_backfill")
    await session.commit()
    await finalize_run(session, run_id, status="succeeded", last_update_cursor=500)
    await session.commit()

    assert await get_last_successful_cursor(session, kinds=SHOW_CURSOR_KINDS) is None


async def test_mark_stale_runs_cancelled(session):
    fresh = await create_run(session, kind="initial")
    stale = await create_run(session, kind="initial")
    await session.commit()

    stale_row = (
        await session.execute(select(m.IngestRun).where(m.IngestRun.id == stale))
    ).scalar_one()
    stale_row.last_progress_at = datetime.now(UTC) - timedelta(hours=1)
    fresh_row = (
        await session.execute(select(m.IngestRun).where(m.IngestRun.id == fresh))
    ).scalar_one()
    fresh_row.last_progress_at = datetime.now(UTC)
    await session.commit()

    cancelled = await mark_stale_runs_cancelled(session, stale_after_minutes=15)
    await session.commit()
    assert cancelled == 1

    stale_row = (
        await session.execute(select(m.IngestRun).where(m.IngestRun.id == stale))
    ).scalar_one()
    fresh_row = (
        await session.execute(select(m.IngestRun).where(m.IngestRun.id == fresh))
    ).scalar_one()
    assert stale_row.status == "cancelled"
    assert fresh_row.status == "running"
