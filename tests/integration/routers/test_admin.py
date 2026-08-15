"""Integration tests for the shared admin surface.

What is left here after NEU-1050 retired the TV Maze triggers is the part no
per-pass test owns: authentication, and the unfiltered `GET /admin/ingest/{id}`
that reports a run of *any* kind. The TMDB pass and delta have their own files
(`test_admin_catalog_ingest.py`, `test_catalog_update.py`), and the per-kind
409 guard has `test_admin_concurrency_guard.py`.
"""

import uuid

import httpx
import pytest
from httpx import ASGITransport

from tvbf.main import app
from tvbf.routers import admin as admin_router


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


async def test_run_status_rejects_unauth(admin_client):
    r = await admin_client.get(f"/admin/ingest/{uuid.uuid4()}")
    assert r.status_code == 401


async def test_run_status_404_for_unknown_run(admin_client):
    fake = uuid.uuid4()
    r = await admin_client.get(f"/admin/ingest/{fake}", headers={"Authorization": "Bearer shh"})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_get_run_status_route_raises_404_for_unknown_run(session):
    """Direct call to admin.get_run_status with an arbitrary UUID."""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        await admin_router.get_run_status(run_id=uuid.uuid4(), session=session)
    assert ei.value.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["catalog_initial", "catalog_update", "update"])
async def test_get_run_status_route_returns_a_run_of_any_kind(session, kind):
    """This route filters on nothing, unlike every per-pass status route.

    That is what a run with no status route of its own is polled through — the
    catalog delta today — and it is why a `update` row written by the retired
    TV Maze daily is still readable while `tvmaze.ingest_run` stands.
    """
    from tvbf.catalog.runs import create_run

    run_id = await create_run(session, kind=kind)
    await session.commit()

    out = await admin_router.get_run_status(run_id=run_id, session=session)
    assert out["id"] == str(run_id)
    assert out["kind"] == kind
    assert out["status"] == "running"
