import uuid

import httpx
import pytest
import respx
from httpx import ASGITransport
from sqlalchemy import select

from tests.fixtures.tvmaze.show_factory import make_show
from tvbf.main import app
from tvbf.tvmaze import models as m


@pytest.fixture
async def admin_client(session, monkeypatch):
    """Async HTTP client that drives the ASGI app in-process.

    Depends on `session` so the conftest truncate teardown runs after each test.
    Stays on the pytest-asyncio session loop, so no engine/pool patching is needed.
    """
    monkeypatch.setenv("ADMIN_TOKEN", "shh")
    from tvbf.config import get_settings

    get_settings.cache_clear()

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
    from tvbf.config import get_settings
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
