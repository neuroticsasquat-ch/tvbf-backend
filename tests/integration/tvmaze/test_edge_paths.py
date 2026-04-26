"""Edge-path tests for tvmaze upsert/ingest/update/browse_queries.

Targets the consecutive-failure abort branches and empty-input early returns
that aren't naturally exercised by happy-path tests.
"""

import asyncio

import httpx
import pytest
import respx
from sqlalchemy import select

from tvbf.tvmaze import models as m
from tvbf.tvmaze.browse_queries import hydrate_show_refs
from tvbf.tvmaze.client import RateLimiter, TVMazeClient
from tvbf.tvmaze.ingest import run_initial_ingest
from tvbf.tvmaze.runs import create_run
from tvbf.tvmaze.update import run_update
from tvbf.tvmaze.upsert import upsert_episodes

# ---------------------------------------------------------------------------
# Empty-input early returns
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_episodes_returns_early_on_empty_list(session):
    """Covers upsert.py:161 — `if not episodes: return`."""
    # Show with show_id 1 doesn't need to exist; the function returns before any DB op.
    await upsert_episodes(session, show_id=1, episodes=[])


@pytest.mark.asyncio
async def test_hydrate_show_refs_returns_empty_for_empty_shows(session):
    """Covers browse_queries.py:143 — `if not shows: return ({}, {}, {})`."""
    g, n, w = await hydrate_show_refs(session, [])
    assert g == {} and n == {} and w == {}


# ---------------------------------------------------------------------------
# Rate-limiter wait branch (client.py:21)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limiter_blocks_when_window_exceeded(monkeypatch):
    """Force the rate limiter to actually wait. We make 2 calls allowed
    per 1-second window, then issue 3 in a row — the 3rd must wait.
    Patch asyncio.sleep so the test is fast."""
    sleeps: list[float] = []

    async def _fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

    limiter = RateLimiter(calls=2, window_seconds=1.0)
    await limiter.acquire()
    await limiter.acquire()
    await limiter.acquire()  # this one must wait

    # At least one sleep was triggered for the third call.
    assert any(s > 0 for s in sleeps)


# ---------------------------------------------------------------------------
# Consecutive-failure abort — initial ingest
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_initial_ingest_aborts_after_threshold_unexpected_failures(session):
    """Each show fetch raises an unexpected exception. With threshold=1, the
    first failure aborts the run — covers ingest.py:83-91 (unexpected-error
    abort branch)."""
    respx.get("https://api.tvmaze.com/updates/shows").mock(
        return_value=httpx.Response(200, json={"1": 100, "2": 200})
    )
    respx.get("https://api.tvmaze.com/shows/1").mock(
        side_effect=Exception("boom")  # not an HTTPStatusError
    )

    run_id = await create_run(session, kind="initial")
    await session.commit()

    async with TVMazeClient(
        "https://api.tvmaze.com", rate_calls=50, rate_window=1, retry_base_delay=0.01
    ) as c:
        result = await run_initial_ingest(
            session_factory=lambda: session,
            client=c,
            run_id=run_id,
            failure_threshold=1,
        )
    # Aborted on the first show; second show never attempted.
    assert result.shows_processed == 0
    assert result.shows_failed == 1
    run = (await session.execute(select(m.IngestRun).where(m.IngestRun.id == run_id))).scalar_one()
    assert run.status == "failed"
    assert "consecutive failures" in (run.error or "")


@respx.mock
@pytest.mark.asyncio
async def test_initial_ingest_aborts_after_threshold_http_failures(session):
    """Each show fetch raises an HTTPStatusError. With threshold=1, abort
    on the first — covers ingest.py:64-73 (HTTP-error abort branch)."""
    respx.get("https://api.tvmaze.com/updates/shows").mock(
        return_value=httpx.Response(200, json={"1": 100})
    )
    respx.get("https://api.tvmaze.com/shows/1").mock(return_value=httpx.Response(500, json={}))

    run_id = await create_run(session, kind="initial")
    await session.commit()

    async with TVMazeClient(
        "https://api.tvmaze.com",
        rate_calls=50,
        rate_window=1,
        retry_base_delay=0.01,
        retry_max_attempts=1,
    ) as c:
        result = await run_initial_ingest(
            session_factory=lambda: session,
            client=c,
            run_id=run_id,
            failure_threshold=1,
        )
    assert result.shows_failed == 1
    run = (await session.execute(select(m.IngestRun).where(m.IngestRun.id == run_id))).scalar_one()
    assert run.status == "failed"


# ---------------------------------------------------------------------------
# Consecutive-failure abort — daily update
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_initial_ingest_aborts_after_threshold_upsert_failures(session):
    """get_show succeeds with invalid JSON → upsert fails → threshold-abort.
    Covers ingest.py:111-118 (the upsert-exception abort branch)."""
    respx.get("https://api.tvmaze.com/updates/shows").mock(
        return_value=httpx.Response(200, json={"1": 100})
    )
    respx.get("https://api.tvmaze.com/shows/1").mock(
        return_value=httpx.Response(200, json={"name": "no-id"})
    )

    run_id = await create_run(session, kind="initial")
    await session.commit()

    async with TVMazeClient(
        "https://api.tvmaze.com", rate_calls=50, rate_window=1, retry_base_delay=0.01
    ) as c:
        result = await run_initial_ingest(
            session_factory=lambda: session,
            client=c,
            run_id=run_id,
            failure_threshold=1,
        )
    assert result.shows_failed == 1
    run = (await session.execute(select(m.IngestRun).where(m.IngestRun.id == run_id))).scalar_one()
    assert run.status == "failed"


@respx.mock
@pytest.mark.asyncio
async def test_run_update_aborts_after_threshold_upsert_failures(session):
    """get_show succeeds with invalid JSON → upsert fails → threshold-abort.
    Covers update.py:70-78 (the upsert-exception abort branch)."""
    respx.get("https://api.tvmaze.com/updates/shows").mock(
        return_value=httpx.Response(200, json={"1": 100})
    )
    # Returns 200 with a payload that fails Pydantic validation (no `id`).
    respx.get("https://api.tvmaze.com/shows/1").mock(
        return_value=httpx.Response(200, json={"name": "no-id"})
    )

    run_id = await create_run(session, kind="update")
    await session.commit()

    async with TVMazeClient(
        "https://api.tvmaze.com", rate_calls=50, rate_window=1, retry_base_delay=0.01
    ) as c:
        result = await run_update(
            session_factory=lambda: session,
            client=c,
            run_id=run_id,
            failure_threshold=1,
        )
    assert result.shows_failed == 1
    run = (await session.execute(select(m.IngestRun).where(m.IngestRun.id == run_id))).scalar_one()
    assert run.status == "failed"
