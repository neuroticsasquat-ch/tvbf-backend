"""The admin surface for the TMDB full-catalog ingest (NEU-1034)."""

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


async def test_post_requires_admin_token(admin_client):
    resp = await admin_client.post("/admin/catalog-ingest")
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_trigger_creates_the_run_and_returns_its_id(session, monkeypatch):
    """202 + run_id, with the pass itself spawned in the background — patched
    here so nothing reaches TMDB."""
    import tvbf.routers.admin as admin_module

    spawned = []

    def _capture(coro):
        spawned.append(coro)
        coro.close()
        return None

    monkeypatch.setattr(admin_module.asyncio, "create_task", _capture)

    out = await admin_router.trigger_catalog_ingest(settings=get_settings(), session=session)
    run_id = uuid.UUID(out["run_id"])
    assert spawned, "trigger_catalog_ingest must spawn the background task"

    row = (
        await session.execute(
            select(m.IngestRun).where(m.IngestRun.id == run_id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()
    assert row.kind == "catalog_initial"


@pytest.mark.asyncio
async def test_a_second_trigger_is_rejected_while_one_is_live(session, monkeypatch):
    """Two passes over 229k series split one 20 req/s budget and each takes twice
    as long — the guard is scoped per kind so a stuck backfill never blocks it."""
    from fastapi import HTTPException

    import tvbf.routers.admin as admin_module

    monkeypatch.setattr(admin_module.asyncio, "create_task", lambda coro: coro.close())

    await admin_router.trigger_catalog_ingest(settings=get_settings(), session=session)

    with pytest.raises(HTTPException) as ei:
        await admin_router.trigger_catalog_ingest(settings=get_settings(), session=session)
    assert ei.value.status_code == 409


@pytest.mark.asyncio
async def test_a_live_run_of_another_kind_does_not_block_this_one(session, monkeypatch):
    """Different kinds, different guards — a live delta must not wedge the pass."""
    import tvbf.routers.admin as admin_module
    from tvbf.tvmaze.runs import create_run

    monkeypatch.setattr(admin_module.asyncio, "create_task", lambda coro: coro.close())

    await create_run(session, kind="catalog_update")
    await session.commit()
    out = await admin_router.trigger_catalog_ingest(settings=get_settings(), session=session)

    assert uuid.UUID(out["run_id"])


async def test_status_returns_404_for_an_unknown_run(admin_client):
    resp = await admin_client.get(
        f"/admin/catalog-ingest/{uuid.uuid4()}", headers={"Authorization": "Bearer shh"}
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_status_returns_404_for_a_run_of_another_kind(session):
    from fastapi import HTTPException

    from tvbf.tvmaze.runs import create_run

    run_id = await create_run(session, kind="akas_backfill")
    await session.commit()

    with pytest.raises(HTTPException) as ei:
        await admin_router.get_catalog_ingest_status(run_id=run_id, session=session)
    assert ei.value.status_code == 404


@pytest.mark.asyncio
async def test_status_reports_progress_for_a_known_run(session):
    from tvbf.tvmaze.runs import create_run, record_progress

    run_id = await create_run(session, kind="catalog_initial")
    await record_progress(session, run_id, processed_delta=12, failed_delta=1)
    await session.commit()

    out = await admin_router.get_catalog_ingest_status(run_id=run_id, session=session)

    assert out["kind"] == "catalog_initial"
    assert out["status"] == "running"
    assert (out["shows_processed"], out["shows_failed"]) == (12, 1)


async def test_the_background_worker_marks_the_run_failed_on_a_crash(session, monkeypatch):
    """A multi-hour pass that died has to say so, or the liveness guard wedges
    this kind until the next container restart."""
    from tvbf.routers.admin import _background_catalog_ingest
    from tvbf.tvmaze.runs import create_run

    async def boom(**kwargs):
        raise RuntimeError("simulated background crash")

    monkeypatch.setattr("tvbf.routers.admin.run_catalog_ingest", boom)
    monkeypatch.setenv("TMDB_READ_ACCESS_TOKEN", "eyJ-not-a-real-token")
    get_settings.cache_clear()

    run_id = await create_run(session, kind="catalog_initial")
    await session.commit()

    await _background_catalog_ingest(run_id, get_settings())

    row = (
        await session.execute(
            select(m.IngestRun).where(m.IngestRun.id == run_id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()
    assert row.status == "failed"
    assert "simulated background crash" in (row.error or "")


async def test_a_missing_tmdb_token_fails_the_run_rather_than_the_process(session, monkeypatch):
    """The token is optional in config so this milestone could land without
    breaking running deploys. The cost is that a misconfigured deploy discovers
    it here — as a failed run row, not a silently dead background task."""
    from tvbf.routers.admin import _background_catalog_ingest
    from tvbf.tvmaze.runs import create_run

    monkeypatch.delenv("TMDB_READ_ACCESS_TOKEN", raising=False)
    get_settings.cache_clear()

    run_id = await create_run(session, kind="catalog_initial")
    await session.commit()

    await _background_catalog_ingest(run_id, get_settings())

    row = (
        await session.execute(
            select(m.IngestRun).where(m.IngestRun.id == run_id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()
    assert row.status == "failed"
    assert "TMDB_READ_ACCESS_TOKEN" in (row.error or "")
