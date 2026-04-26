"""Integration tests for admin routes.

Merges tests from tests/test_admin_routes.py and the admin handler tests from
tests/test_route_handlers_browse_admin.py.
"""

import uuid

import httpx
import pytest
import respx
from httpx import ASGITransport
from sqlalchemy import select

from tests.fixtures.tvmaze.show_factory import make_show
from tvbf.config import get_settings
from tvbf.main import app
from tvbf.routers import admin as admin_router
from tvbf.tvmaze import models as m


@pytest.fixture
async def admin_client(session, monkeypatch):
    """Async HTTP client that drives the ASGI app in-process.

    Depends on `session` so the conftest truncate teardown runs after each test.
    Stays on the pytest-asyncio session loop, so no engine/pool patching is needed.
    """
    monkeypatch.setenv("ADMIN_TOKEN", "shh")
    from tvbf.config import get_settings as _get_settings

    _get_settings.cache_clear()

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


async def test_ingest_rejects_unauth(admin_client):
    r = await admin_client.post("/admin/ingest")
    assert r.status_code == 401


@respx.mock
async def test_update_runs_synchronously(admin_client):
    respx.get("https://api.tvmaze.com/updates/shows").mock(
        return_value=httpx.Response(200, json={"1": 10})
    )
    respx.get("https://api.tvmaze.com/shows/1").mock(
        return_value=httpx.Response(200, json=make_show(1, 10))
    )
    r = await admin_client.post("/admin/update", headers={"Authorization": "Bearer shh"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["shows_processed"] == 1
    assert body["last_update_cursor"] == 10


@respx.mock
async def test_ingest_accepts_and_returns_run_id(admin_client):
    respx.get("https://api.tvmaze.com/updates/shows").mock(
        return_value=httpx.Response(200, json={})
    )
    r = await admin_client.post("/admin/ingest", headers={"Authorization": "Bearer shh"})
    assert r.status_code == 202, r.text
    assert "run_id" in r.json()


async def test_ingest_status_404_for_unknown_run(admin_client):
    fake = uuid.uuid4()
    r = await admin_client.get(f"/admin/ingest/{fake}", headers={"Authorization": "Bearer shh"})
    assert r.status_code == 404


async def test_background_ingest_marks_run_failed_on_crash(session, monkeypatch):
    from tvbf.routers.admin import _background_ingest
    from tvbf.tvmaze.runs import create_run

    async def boom(**kwargs):
        raise RuntimeError("simulated background crash")

    monkeypatch.setattr("tvbf.routers.admin.run_initial_ingest", boom)

    run_id = await create_run(session, kind="initial")
    await session.commit()

    await _background_ingest(run_id, get_settings())

    row = (
        await session.execute(
            select(m.IngestRun).where(m.IngestRun.id == run_id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()
    assert row.status == "failed"
    assert row.error is not None
    assert "simulated background crash" in row.error


# ---------------------------------------------------------------------------
# Admin — direct route handler calls (from test_route_handlers_browse_admin.py)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_run_status_route_raises_404_for_unknown_run(session):
    """Direct call to admin.get_run_status with an arbitrary UUID."""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        await admin_router.get_run_status(run_id=uuid.uuid4(), session=session)
    assert ei.value.status_code == 404


@pytest.mark.asyncio
async def test_get_run_status_route_returns_run_for_known_id(session):
    """Seed a run and read it back via the route handler."""
    from tvbf.tvmaze.runs import create_run

    run_id = await create_run(session, kind="initial")
    await session.commit()

    out = await admin_router.get_run_status(run_id=run_id, session=session)
    assert out["id"] == str(run_id)
    assert out["kind"] == "initial"
    assert out["status"] == "running"


@pytest.mark.asyncio
async def test_trigger_ingest_route_creates_run_and_returns_id(session, monkeypatch):
    """trigger_ingest spawns a background task. Patch asyncio.create_task to
    a no-op so the test doesn't actually run ingestion against TV Maze."""
    import tvbf.routers.admin as admin_module

    spawned = []

    def _capture(coro):
        # Close the coroutine to avoid 'never awaited' warning.
        spawned.append(coro)
        coro.close()
        return None

    monkeypatch.setattr(admin_module.asyncio, "create_task", _capture)

    settings = get_settings()
    out = await admin_router.trigger_ingest(settings=settings, session=session)
    assert "run_id" in out
    assert spawned, "trigger_ingest must spawn the background task"
