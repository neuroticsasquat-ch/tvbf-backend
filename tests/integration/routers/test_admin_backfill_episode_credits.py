"""Integration tests for the episode-credits backfill admin routes."""

import uuid

import httpx
import pytest
from httpx import ASGITransport
from sqlalchemy import select

from tvbf.config import get_settings
from tvbf.main import app
from tvbf.routers import admin as admin_router
from tvbf.tvmaze import models as m


@pytest.fixture
async def admin_client(session, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "shh")
    from tvbf.config import get_settings as _get_settings

    _get_settings.cache_clear()

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


async def test_backfill_post_requires_admin_token(admin_client):
    resp = await admin_client.post("/admin/backfill-episode-credits")
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_trigger_creates_run_and_returns_id(session, monkeypatch):
    """The route spawns a background task; patch create_task so nothing hits TV Maze."""
    import tvbf.routers.admin as admin_module

    spawned = []

    def _capture(coro):
        spawned.append(coro)
        coro.close()
        return None

    monkeypatch.setattr(admin_module.asyncio, "create_task", _capture)

    out = await admin_router.trigger_backfill_episode_credits(
        settings=get_settings(), session=session
    )
    run_id = uuid.UUID(out["run_id"])
    assert spawned, "trigger_backfill_episode_credits must spawn the background task"

    row = (
        await session.execute(
            select(m.IngestRun).where(m.IngestRun.id == run_id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()
    assert row.kind == "episode_credits_backfill"


async def test_status_returns_404_for_missing(admin_client):
    fake = uuid.uuid4()
    resp = await admin_client.get(
        f"/admin/backfill-episode-credits/{fake}", headers={"Authorization": "Bearer shh"}
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_status_returns_404_for_wrong_kind(session):
    from fastapi import HTTPException

    from tvbf.tvmaze.runs import create_run

    run_id = await create_run(session, kind="akas_backfill")
    await session.commit()

    with pytest.raises(HTTPException) as ei:
        await admin_router.get_backfill_episode_credits_status(run_id=run_id, session=session)
    assert ei.value.status_code == 404


@pytest.mark.asyncio
async def test_status_returns_run_for_known_id(session):
    from tvbf.tvmaze.runs import create_run

    run_id = await create_run(session, kind="episode_credits_backfill")
    await session.commit()

    out = await admin_router.get_backfill_episode_credits_status(run_id=run_id, session=session)
    assert out["id"] == str(run_id)
    assert out["kind"] == "episode_credits_backfill"
    assert out["status"] == "running"


@pytest.mark.asyncio
async def test_second_trigger_is_rejected_while_one_is_live(session, monkeypatch):
    """NEU-966's guard is scoped per kind, and this kind is the reason it exists:
    two 29-hour passes split one process-wide rate limit and each takes 58 hours."""
    from fastapi import HTTPException

    import tvbf.routers.admin as admin_module

    monkeypatch.setattr(admin_module.asyncio, "create_task", lambda coro: coro.close())

    await admin_router.trigger_backfill_episode_credits(settings=get_settings(), session=session)

    with pytest.raises(HTTPException) as ei:
        await admin_router.trigger_backfill_episode_credits(
            settings=get_settings(), session=session
        )
    assert ei.value.status_code == 409


async def test_background_worker_marks_run_failed_on_crash(session, monkeypatch):
    from tvbf.routers.admin import _background_backfill_episode_credits
    from tvbf.tvmaze.runs import create_run

    async def boom(**kwargs):
        raise RuntimeError("simulated background crash")

    monkeypatch.setattr("tvbf.routers.admin.run_episode_credits_backfill", boom)

    run_id = await create_run(session, kind="episode_credits_backfill")
    await session.commit()

    await _background_backfill_episode_credits(run_id, get_settings())

    row = (
        await session.execute(
            select(m.IngestRun).where(m.IngestRun.id == run_id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()
    assert row.status == "failed"
    assert row.error is not None
    assert "simulated background crash" in row.error
