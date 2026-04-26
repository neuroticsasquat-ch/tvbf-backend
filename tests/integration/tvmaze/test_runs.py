from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select

from tvbf.tvmaze import models as m
from tvbf.tvmaze.runs import (
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
    r1 = await create_run(session, kind="initial")
    r2 = await create_run(session, kind="update")
    await session.commit()
    await finalize_run(session, r1, status="succeeded", last_update_cursor=10)
    await finalize_run(session, r2, status="succeeded", last_update_cursor=20)
    await session.commit()
    assert await get_last_successful_cursor(session) == 20


async def test_get_last_successful_cursor_none_when_no_runs(session):
    assert await get_last_successful_cursor(session) is None


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
