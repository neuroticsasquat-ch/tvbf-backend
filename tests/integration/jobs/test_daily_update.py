"""The daily-update CLI entrypoint (`python -m tvbf.jobs.daily_update`).

The exit code is the contract Coolify reads, and the deadman pings are what
catch a daily that never runs at all — so both are pinned here.
"""

import httpx
import pytest
import respx
from sqlalchemy import select

from tvbf.config import get_settings
from tvbf.db import SessionLocal
from tvbf.jobs import daily_update
from tvbf.tvmaze import models as m
from tvbf.tvmaze.runs import create_run, finalize_run

HEALTHCHECK = "https://hc.example.com/abc123"


def _settings(**overrides):
    return get_settings().model_copy(update=overrides)


async def _finalize(run_id, status: str) -> None:
    async with SessionLocal() as s:
        await finalize_run(s, run_id, status=status)
        await s.commit()


@pytest.fixture
def worker(monkeypatch):
    """Replace the update body with one that finalizes to a status we choose.

    The daily's own logic is the guard, the run row and the exit code; what
    `run_update` does is covered by its own tests.
    """

    def _install(status: str):
        async def _fake(run_id, settings):
            await _finalize(run_id, status)

        monkeypatch.setattr(daily_update, "run_update_job", _fake)

    return _install


@respx.mock
async def test_a_successful_run_exits_zero_and_pings_start_then_success(session, worker):
    worker("succeeded")
    start = respx.post(f"{HEALTHCHECK}/start").mock(return_value=httpx.Response(200))
    success = respx.post(HEALTHCHECK).mock(return_value=httpx.Response(200))

    assert await daily_update.run_daily(_settings(healthcheck_daily_url=HEALTHCHECK)) is True
    assert start.called
    assert success.called


@respx.mock
async def test_a_failed_run_exits_nonzero_and_pings_fail(session, worker):
    worker("failed")
    respx.post(f"{HEALTHCHECK}/start").mock(return_value=httpx.Response(200))
    fail = respx.post(f"{HEALTHCHECK}/fail").mock(return_value=httpx.Response(200))

    assert await daily_update.run_daily(_settings(healthcheck_daily_url=HEALTHCHECK)) is False
    assert fail.called


@respx.mock
async def test_a_cancelled_run_also_exits_nonzero(session, worker):
    worker("cancelled")
    respx.post(f"{HEALTHCHECK}/start").mock(return_value=httpx.Response(200))
    fail = respx.post(f"{HEALTHCHECK}/fail").mock(return_value=httpx.Response(200))

    assert await daily_update.run_daily(_settings(healthcheck_daily_url=HEALTHCHECK)) is False
    assert fail.called


@respx.mock
async def test_an_unset_healthcheck_url_attempts_no_request(session, worker):
    """Local runs and tests must never call out. respx would raise on any
    unmocked request, so reaching the end at all is the assertion."""
    worker("succeeded")

    assert await daily_update.run_daily(_settings(healthcheck_daily_url=None)) is True
    assert not respx.calls


@respx.mock
async def test_a_ping_that_fails_is_swallowed(session, worker, caplog):
    """A ping that cannot get out is itself what the deadman alerts on, so it
    must not change the job's own outcome."""
    worker("succeeded")
    respx.post(f"{HEALTHCHECK}/start").mock(side_effect=httpx.ConnectError("no route"))
    respx.post(HEALTHCHECK).mock(side_effect=httpx.ConnectError("no route"))

    assert await daily_update.run_daily(_settings(healthcheck_daily_url=HEALTHCHECK)) is True
    assert "healthcheck ping" in caplog.text


@respx.mock
async def test_a_live_update_run_is_left_alone_and_reports_no_outcome(session, monkeypatch):
    """An operator triggered a daily minutes before the schedule fired.

    Exit 0 — this task did nothing wrong. But no success ping either: the
    hand-triggered run pings nothing itself, so claiming success here would feed
    the deadman for a run whose outcome we never learn. Leaving the check in its
    started state trades a spurious alert on a rare day for never swallowing a
    failed one.
    """

    async def _must_not_run(run_id, settings):
        raise AssertionError("started a second update run alongside a live one")

    monkeypatch.setattr(daily_update, "run_update_job", _must_not_run)
    live_id = await create_run(session, kind="update")
    await session.commit()

    start = respx.post(f"{HEALTHCHECK}/start").mock(return_value=httpx.Response(200))

    # Neither the base URL nor `/fail` is mocked: respx raises on an unmocked
    # request, so a ping to either fails this test rather than passing silently.
    assert await daily_update.run_daily(_settings(healthcheck_daily_url=HEALTHCHECK)) is True
    assert start.called
    assert len(respx.calls) == 1, "the skip path pinged something beyond /start"

    runs = (await session.execute(select(m.IngestRun.id))).scalars().all()
    assert runs == [live_id], "the live run should be the only one"


def test_main_maps_the_outcome_to_an_exit_code(monkeypatch):
    """0 = the daily ran and succeeded; 1 = it failed. Coolify reads this."""

    async def _ok(settings):
        return True

    async def _bad(settings):
        return False

    monkeypatch.setattr(daily_update, "run_daily", _ok)
    assert daily_update.main() == 0

    monkeypatch.setattr(daily_update, "run_daily", _bad)
    assert daily_update.main() == 1


def test_main_exits_nonzero_when_the_job_crashes_outside_the_run(monkeypatch):
    """The guard query, the run insert and the status read all sit outside
    `run_update_job`, so a failure there leaves no `failed` row to speak for it."""

    async def _boom(settings):
        raise RuntimeError("database is down")

    monkeypatch.setattr(daily_update, "run_daily", _boom)
    assert daily_update.main() == 1
