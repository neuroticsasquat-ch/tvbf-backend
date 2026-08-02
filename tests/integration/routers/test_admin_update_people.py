"""Integration tests for the daily person delta admin route — NEU-943."""

import uuid

import httpx
import pytest
import respx
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


@respx.mock
async def test_update_people_post_returns_202_and_creates_run(admin_client, session):
    resp = await admin_client.post("/admin/update-people", headers={"Authorization": "Bearer shh"})
    assert resp.status_code == 202, resp.text
    run_id = uuid.UUID(resp.json()["run_id"])

    row = (
        await session.execute(
            select(m.IngestRun).where(m.IngestRun.id == run_id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()
    assert row.kind == "person_update"
    assert row.status in ("running", "succeeded", "failed")


async def test_update_people_post_requires_admin_token(admin_client):
    resp = await admin_client.post("/admin/update-people")
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_trigger_person_update_spawns_the_background_task(session, monkeypatch):
    """Patch asyncio.create_task to a no-op so the test doesn't hit TV Maze."""
    import tvbf.routers.admin as admin_module

    spawned = []

    def _capture(coro):
        spawned.append(coro)
        coro.close()
        return None

    monkeypatch.setattr(admin_module.asyncio, "create_task", _capture)

    out = await admin_router.trigger_person_update(settings=get_settings(), session=session)
    assert "run_id" in out
    assert spawned, "trigger_person_update must spawn the background task"


async def test_background_person_update_marks_run_failed_on_crash(session, monkeypatch):
    from tvbf.routers.admin import _background_person_update
    from tvbf.tvmaze.runs import create_run

    async def boom(**kwargs):
        raise RuntimeError("simulated background crash")

    monkeypatch.setattr("tvbf.routers.admin.run_person_update", boom)

    run_id = await create_run(session, kind="person_update")
    await session.commit()

    await _background_person_update(run_id, get_settings())

    row = (
        await session.execute(
            select(m.IngestRun).where(m.IngestRun.id == run_id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()
    assert row.status == "failed"
    assert row.error is not None
    assert "simulated background crash" in row.error
