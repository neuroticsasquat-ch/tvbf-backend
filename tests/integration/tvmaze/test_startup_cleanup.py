from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from tvbf.main import run_startup_cleanup
from tvbf.tvmaze import models as m


async def test_startup_cleanup_cancels_stale_running_runs(session):
    stale = m.IngestRun(kind="initial", status="running")
    stale.last_progress_at = datetime.now(UTC) - timedelta(hours=2)
    session.add(stale)
    fresh = m.IngestRun(kind="initial", status="running")
    fresh.last_progress_at = datetime.now(UTC)
    session.add(fresh)
    await session.commit()

    await run_startup_cleanup(session, stale_after_minutes=15)
    await session.commit()

    rows = (await session.execute(select(m.IngestRun))).scalars().all()
    by_status = {r.status: r for r in rows}
    assert "cancelled" in by_status
    assert "running" in by_status


async def test_startup_cleanup_cancels_stale_akas_backfill_runs(session):
    stale = m.IngestRun(kind="akas_backfill", status="running")
    stale.last_progress_at = datetime.now(UTC) - timedelta(hours=2)
    session.add(stale)
    await session.commit()

    await run_startup_cleanup(session, stale_after_minutes=15)
    await session.commit()

    await session.refresh(stale)
    assert stale.status == "cancelled"
    assert stale.finished_at is not None
