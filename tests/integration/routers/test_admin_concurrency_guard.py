"""Every admin POST refuses to start a second run of the same kind.

Nothing used to stop this: each route called `asyncio.create_task`
unconditionally. A stray second POST during a long pass walks the same work
list twice, and since the request budget is shared per upstream (NEU-955,
ADR-0006) the two runs simply split one allowance — a ~8.7h pass becomes
~17h. See NEU-966.
"""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from httpx import ASGITransport
from sqlalchemy import select

from tvbf.catalog import models as m
from tvbf.catalog.runs import create_run
from tvbf.main import app

# Every POST route on the admin router, with the run kind it creates.
ROUTE_KINDS = [
    ("/admin/catalog-ingest", "catalog_initial"),
    ("/admin/catalog-update", "catalog_update"),
]

AUTH = {"Authorization": "Bearer shh"}


BACKGROUND_FNS = [
    "_background_catalog_ingest",
    # Shared with the `tvbf.jobs.catalog_update` CLI, so it lives in `update.py`
    # rather than the router — but it is still bound as a name in the router's
    # module namespace, which is what `setattr` needs (NEU-1035).
    "run_catalog_update_job",
]


@pytest.fixture
async def admin_client(session, monkeypatch):
    """Admin client whose background work is stubbed to a no-op.

    Stubs the `_background_*` wrappers rather than `asyncio.create_task`:
    `admin_module.asyncio` *is* the shared asyncio module, so patching
    `create_task` there patches it process-wide and breaks Starlette's own
    internals mid-request. Other admin tests get away with it only because
    they call the route functions directly instead of through the ASGI stack.

    The guard is decided before the task is spawned, so a no-op body changes
    nothing about what is under test.
    """
    monkeypatch.setenv("ADMIN_TOKEN", "shh")
    from tvbf.config import get_settings as _get_settings

    _get_settings.cache_clear()

    import tvbf.routers.admin as admin_module

    async def _noop(run_id, settings):
        return None

    for name in BACKGROUND_FNS:
        monkeypatch.setattr(admin_module, name, _noop)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


async def _backdate_start(session, run_id, delta: timedelta) -> None:
    row = (await session.execute(select(m.IngestRun).where(m.IngestRun.id == run_id))).scalar_one()
    row.started_at = datetime.now(UTC) - delta
    await session.commit()


@pytest.mark.parametrize("route,kind", ROUTE_KINDS)
async def test_post_409s_when_a_run_of_that_kind_is_live(admin_client, session, route, kind):
    run_id = await create_run(session, kind=kind)
    await session.commit()

    resp = await admin_client.post(route, headers=AUTH)

    assert resp.status_code == 409, resp.text
    # The operator needs to know which run to go and poll.
    assert str(run_id) in resp.json()["detail"]


@pytest.mark.parametrize("route,kind", ROUTE_KINDS)
async def test_post_202s_when_the_live_run_is_a_different_kind(admin_client, session, route, kind):
    """Scoped to one kind: a stuck full pass must not block an urgent delta."""
    other = next(k for _, k in ROUTE_KINDS if k != kind)
    await create_run(session, kind=other)
    await session.commit()

    resp = await admin_client.post(route, headers=AUTH)

    assert resp.status_code == 202, resp.text


@pytest.mark.parametrize("route,kind", ROUTE_KINDS)
async def test_post_202s_when_the_running_run_of_that_kind_is_stale(
    admin_client, session, route, kind
):
    """A run whose process died still reads `running` until the next restart.

    `mark_stale_runs_cancelled` fires only in the lifespan hook, so without the
    staleness clause this row would wedge its kind indefinitely.
    """
    run_id = await create_run(session, kind=kind)
    await session.commit()
    await _backdate_start(session, run_id, timedelta(hours=1))

    resp = await admin_client.post(route, headers=AUTH)

    assert resp.status_code == 202, resp.text


@pytest.mark.parametrize("route,kind", ROUTE_KINDS)
async def test_post_202s_when_the_previous_run_finished(admin_client, session, route, kind):
    from tvbf.catalog.runs import finalize_run

    run_id = await create_run(session, kind=kind)
    await session.commit()
    await finalize_run(session, run_id, status="succeeded")
    await session.commit()

    resp = await admin_client.post(route, headers=AUTH)

    assert resp.status_code == 202, resp.text


async def test_409_does_not_create_a_second_run_row(admin_client, session):
    """The rejection must happen before `create_run`, not after."""
    await create_run(session, kind="catalog_initial")
    await session.commit()

    resp = await admin_client.post("/admin/catalog-ingest", headers=AUTH)
    assert resp.status_code == 409

    rows = (
        (await session.execute(select(m.IngestRun).where(m.IngestRun.kind == "catalog_initial")))
        .scalars()
        .all()
    )
    assert len(rows) == 1


async def test_guard_runs_after_auth(admin_client, session):
    """An unauthenticated caller learns nothing about what is running."""
    await create_run(session, kind="catalog_initial")
    await session.commit()

    resp = await admin_client.post("/admin/catalog-ingest")

    assert resp.status_code in (401, 403)
