"""The catalog-delta CLI entrypoint (`python -m tvbf.jobs.catalog_update`).

Same contract as the TV Maze daily this outlived (NEU-1050), and since that
one's tests went with it, this file is now the only cover for the shape both
shared — `tvbf.jobs.scheduled`. The exit code is what Coolify reads and the
deadman pings are what catch a task that never runs at all, so both are pinned
here, including the two paths only the daily's file used to reach.
"""

import httpx
import pytest
import respx
from sqlalchemy import select

from tvbf.config import get_settings
from tvbf.db import SessionLocal
from tvbf.jobs import catalog_update
from tvbf.tvmaze import models as m
from tvbf.tvmaze.runs import create_run, finalize_run

HEALTHCHECK = "https://hc.example.com/catalog"


def _settings(**overrides):
    return get_settings().model_copy(update=overrides)


@pytest.fixture
def worker(monkeypatch):
    """Replace the delta body with one that finalizes to a status we choose.

    What `run_catalog_update` does is covered by its own tests; the CLI's own
    logic is the guard, the run row, the pings and the exit code.
    """

    def _install(status: str):
        async def _fake(run_id, settings):
            async with SessionLocal() as s:
                await finalize_run(s, run_id, status=status)
                await s.commit()

        monkeypatch.setattr(catalog_update, "run_catalog_update_job", _fake)

    return _install


@respx.mock
async def test_a_successful_run_exits_zero_and_pings_start_then_success(session, worker):
    worker("succeeded")
    start = respx.post(f"{HEALTHCHECK}/start").mock(return_value=httpx.Response(200))
    success = respx.post(HEALTHCHECK).mock(return_value=httpx.Response(200))

    assert (
        await catalog_update.run_catalog_daily(_settings(healthcheck_catalog_url=HEALTHCHECK))
        is True
    )
    assert start.called
    assert success.called


@respx.mock
async def test_a_failed_run_exits_nonzero_and_pings_fail(session, worker):
    worker("failed")
    respx.post(f"{HEALTHCHECK}/start").mock(return_value=httpx.Response(200))
    fail = respx.post(f"{HEALTHCHECK}/fail").mock(return_value=httpx.Response(200))

    assert (
        await catalog_update.run_catalog_daily(_settings(healthcheck_catalog_url=HEALTHCHECK))
        is False
    )
    assert fail.called


@respx.mock
async def test_the_run_row_is_created_with_the_catalog_update_kind(session, worker):
    """The kind is what the concurrency guard and the cursor lineage both key
    off, so a typo here would silently give the delta its own private world."""
    worker("succeeded")

    assert await catalog_update.run_catalog_daily(_settings(healthcheck_catalog_url=None)) is True

    kinds = (await session.execute(select(m.IngestRun.kind))).scalars().all()
    assert kinds == ["catalog_update"]


@respx.mock
async def test_a_live_catalog_run_is_left_alone_and_reports_no_outcome(session, monkeypatch):
    """An operator triggered a delta by hand minutes before the schedule fired.

    Exit 0 with no success ping: the hand-triggered run pings nothing itself, so
    claiming success here would feed the deadman for a run whose outcome we
    never learn.
    """

    async def _must_not_run(run_id, settings):
        raise AssertionError("started a second delta alongside a live one")

    monkeypatch.setattr(catalog_update, "run_catalog_update_job", _must_not_run)
    live_id = await create_run(session, kind="catalog_update")
    await session.commit()

    start = respx.post(f"{HEALTHCHECK}/start").mock(return_value=httpx.Response(200))

    assert (
        await catalog_update.run_catalog_daily(_settings(healthcheck_catalog_url=HEALTHCHECK))
        is True
    )
    assert start.called
    assert len(respx.calls) == 1, "the skip path pinged something beyond /start"

    runs = (await session.execute(select(m.IngestRun.id))).scalars().all()
    assert runs == [live_id]


@respx.mock
async def test_a_live_run_of_another_kind_does_not_block_the_catalog_delta(session, worker):
    """Different kinds, different guards.

    The stand-in here is an `update` row, the kind the retired TV Maze daily
    wrote: those rows still stand in `ingest_run`, and one left `running` by a
    process that died must never wedge the delta that replaced it.
    """
    worker("succeeded")
    await create_run(session, kind="update")
    await session.commit()

    assert await catalog_update.run_catalog_daily(_settings(healthcheck_catalog_url=None)) is True

    kinds = sorted((await session.execute(select(m.IngestRun.kind))).scalars().all())
    assert kinds == ["catalog_update", "update"]


@respx.mock
async def test_an_unset_healthcheck_url_attempts_no_request(session, worker):
    """Local runs and tests must never call out. respx would raise on any
    unmocked request, so reaching the end at all is the assertion."""
    worker("succeeded")

    assert await catalog_update.run_catalog_daily(_settings(healthcheck_catalog_url=None)) is True
    assert not respx.calls


@respx.mock
async def test_a_cancelled_run_also_exits_nonzero(session, worker):
    """`succeeded` is the only outcome that means the delta ran. A run the
    startup cleanup cancelled did not, and Coolify has to hear about it."""
    worker("cancelled")
    respx.post(f"{HEALTHCHECK}/start").mock(return_value=httpx.Response(200))
    fail = respx.post(f"{HEALTHCHECK}/fail").mock(return_value=httpx.Response(200))

    assert (
        await catalog_update.run_catalog_daily(_settings(healthcheck_catalog_url=HEALTHCHECK))
        is False
    )
    assert fail.called


@respx.mock
async def test_a_ping_that_fails_is_swallowed(session, worker, caplog):
    """A ping that cannot get out is itself what the deadman alerts on, so it
    must not change the job's own outcome."""
    worker("succeeded")
    respx.post(f"{HEALTHCHECK}/start").mock(side_effect=httpx.ConnectError("no route"))
    respx.post(HEALTHCHECK).mock(side_effect=httpx.ConnectError("no route"))

    assert (
        await catalog_update.run_catalog_daily(_settings(healthcheck_catalog_url=HEALTHCHECK))
        is True
    )
    assert "healthcheck ping" in caplog.text


def test_main_maps_the_outcome_to_an_exit_code(monkeypatch):
    """0 = the delta ran and succeeded; 1 = it failed. Coolify reads this."""

    async def _ok(settings):
        return True

    async def _bad(settings):
        return False

    monkeypatch.setattr(catalog_update, "run_catalog_daily", _ok)
    assert catalog_update.main() == 0

    monkeypatch.setattr(catalog_update, "run_catalog_daily", _bad)
    assert catalog_update.main() == 1


@respx.mock
def test_main_pings_fail_when_the_job_crashes_outside_the_run(monkeypatch):
    """The guard query, the run insert and the status read all sit outside the
    delta itself, so a failure there leaves no `failed` row to speak for it."""
    monkeypatch.setenv("HEALTHCHECK_CATALOG_URL", HEALTHCHECK)
    get_settings.cache_clear()
    fail = respx.post(f"{HEALTHCHECK}/fail").mock(return_value=httpx.Response(200))

    async def _boom(settings):
        raise RuntimeError("database is down")

    monkeypatch.setattr(catalog_update, "run_catalog_daily", _boom)
    try:
        assert catalog_update.main() == 1
        assert fail.called
    finally:
        get_settings.cache_clear()
