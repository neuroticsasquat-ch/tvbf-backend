"""Integration tests for AKA backfill admin routes."""

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
async def test_backfill_post_returns_202_and_creates_run(admin_client, session):
    """POST /admin/backfill-akas returns 202 + run_id and persists an IngestRun row."""
    resp = await admin_client.post("/admin/backfill-akas", headers={"Authorization": "Bearer shh"})
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert "run_id" in body
    run_id = uuid.UUID(body["run_id"])

    row = (
        await session.execute(
            select(m.IngestRun).where(m.IngestRun.id == run_id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()
    assert row.kind == "akas_backfill"
    assert row.status in ("running", "succeeded", "failed")


@pytest.mark.asyncio
async def test_trigger_backfill_akas_route_creates_run_and_returns_id(session, monkeypatch):
    """trigger_backfill_akas spawns a background task. Patch asyncio.create_task
    to a no-op so the test doesn't actually hit TV Maze."""
    import tvbf.routers.admin as admin_module

    spawned = []

    def _capture(coro):
        spawned.append(coro)
        coro.close()
        return None

    monkeypatch.setattr(admin_module.asyncio, "create_task", _capture)

    settings = get_settings()
    out = await admin_router.trigger_backfill_akas(settings=settings, session=session)
    assert "run_id" in out
    assert spawned, "trigger_backfill_akas must spawn the background task"


async def test_backfill_status_returns_404_for_missing(admin_client):
    fake = uuid.uuid4()
    resp = await admin_client.get(
        f"/admin/backfill-akas/{fake}", headers={"Authorization": "Bearer shh"}
    )
    assert resp.status_code == 404


async def test_backfill_post_requires_admin_token(admin_client):
    resp = await admin_client.post("/admin/backfill-akas")
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_backfill_status_returns_404_for_wrong_kind(session):
    """A run with kind != 'akas_backfill' must 404 from the backfill status route."""
    from fastapi import HTTPException

    from tvbf.tvmaze.runs import create_run

    run_id = await create_run(session, kind="initial")
    await session.commit()

    with pytest.raises(HTTPException) as ei:
        await admin_router.get_backfill_akas_status(run_id=run_id, session=session)
    assert ei.value.status_code == 404


@pytest.mark.asyncio
async def test_backfill_status_returns_run_for_known_id(session):
    from tvbf.tvmaze.runs import create_run

    run_id = await create_run(session, kind="akas_backfill")
    await session.commit()

    out = await admin_router.get_backfill_akas_status(run_id=run_id, session=session)
    assert out["id"] == str(run_id)
    assert out["kind"] == "akas_backfill"
    assert out["status"] == "running"


async def test_background_backfill_akas_marks_run_failed_on_crash(session, monkeypatch):
    from tvbf.routers.admin import _background_backfill_akas
    from tvbf.tvmaze.runs import create_run

    async def boom(**kwargs):
        raise RuntimeError("simulated background crash")

    monkeypatch.setattr("tvbf.routers.admin.run_akas_backfill", boom)

    run_id = await create_run(session, kind="akas_backfill")
    await session.commit()

    await _background_backfill_akas(run_id, get_settings())

    row = (
        await session.execute(
            select(m.IngestRun).where(m.IngestRun.id == run_id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()
    assert row.status == "failed"
    assert row.error is not None
    assert "simulated background crash" in row.error
