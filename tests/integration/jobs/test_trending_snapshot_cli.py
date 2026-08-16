"""The trending snapshot's scheduled entrypoint (NEU-1055).

Its own deadman, not the catalog delta's, and its own run kind. What the pass
itself does is covered in `tests/integration/tmdb/test_trending.py`; what is
here is the contract Coolify and healthchecks.io read — the exit code, the
pings, and the per-kind in-flight guard.
"""

import httpx
import respx
from sqlalchemy import select

from tvbf.catalog import models as m
from tvbf.catalog.runs import create_run, finalize_run
from tvbf.config import get_settings
from tvbf.db import SessionLocal
from tvbf.jobs import trending_snapshot

HEALTHCHECK = "https://hc.example.com/trending"
CATALOG_HEALTHCHECK = "https://hc.example.com/catalog"


def _settings(**overrides):
    return get_settings().model_copy(update=overrides)


def _install(monkeypatch, status: str):
    async def _fake(run_id, settings):
        async with SessionLocal() as s:
            await finalize_run(s, run_id, status=status)
            await s.commit()

    monkeypatch.setattr(trending_snapshot, "run_trending_snapshot_job", _fake)


@respx.mock
async def test_a_successful_run_exits_zero_and_pings_its_own_check(session, monkeypatch):
    """Its own check, never another task's: one deadman fed by two scheduled
    tasks is kept alive by either of them, so the day one stops running the
    other goes on covering for it silently."""
    _install(monkeypatch, "succeeded")
    start = respx.post(f"{HEALTHCHECK}/start").mock(return_value=httpx.Response(200))
    success = respx.post(HEALTHCHECK).mock(return_value=httpx.Response(200))
    catalog = respx.post(CATALOG_HEALTHCHECK).mock(return_value=httpx.Response(200))

    assert (
        await trending_snapshot.run_trending_daily(
            _settings(
                healthcheck_trending_url=HEALTHCHECK,
                healthcheck_catalog_url=CATALOG_HEALTHCHECK,
            )
        )
        is True
    )
    assert start.called and success.called
    assert not catalog.called


@respx.mock
async def test_a_failed_run_reports_false_and_pings_fail(session, monkeypatch):
    """A snapshot that resolved nothing finalizes `failed`, which is what makes
    "the previous snapshot was kept" reach Coolify rather than pass for a quiet
    success."""
    _install(monkeypatch, "failed")
    respx.post(f"{HEALTHCHECK}/start").mock(return_value=httpx.Response(200))
    fail = respx.post(f"{HEALTHCHECK}/fail").mock(return_value=httpx.Response(200))

    assert (
        await trending_snapshot.run_trending_daily(_settings(healthcheck_trending_url=HEALTHCHECK))
        is False
    )
    assert fail.called


async def test_the_run_it_creates_carries_its_own_kind(session, monkeypatch):
    """`trending_snapshot` had to be admitted to `ck_ingest_run_kind`, and this
    is what would fail if the migration and the model ever disagreed about it."""
    _install(monkeypatch, "succeeded")

    assert await trending_snapshot.run_trending_daily(_settings()) is True

    kinds = (
        (await session.execute(select(m.IngestRun.kind).order_by(m.IngestRun.started_at)))
        .scalars()
        .all()
    )
    assert kinds == ["trending_snapshot"]


async def test_a_run_already_in_flight_is_left_alone(session, monkeypatch):
    """Someone triggered it by hand minutes before the schedule fired. Exiting 0
    is right — this task did nothing wrong — and a second snapshot would spend a
    request to overwrite a list minutes old."""
    ran = False

    async def _fake(run_id, settings):
        nonlocal ran
        ran = True

    monkeypatch.setattr(trending_snapshot, "run_trending_snapshot_job", _fake)
    await create_run(session, kind="trending_snapshot")
    await session.commit()

    assert await trending_snapshot.run_trending_daily(_settings()) is True
    assert ran is False


async def test_a_catalog_delta_in_flight_does_not_block_this_snapshot(session, monkeypatch):
    """The guard is per kind. This pass is deliberately not ordered after the
    delta — an entry mirrored tonight is dropped from today's snapshot either
    way, and coupling the two would buy a day's coverage of a case measured at
    zero occurrences."""
    _install(monkeypatch, "succeeded")
    await create_run(session, kind="catalog_update")
    await session.commit()

    assert await trending_snapshot.run_trending_daily(_settings()) is True
