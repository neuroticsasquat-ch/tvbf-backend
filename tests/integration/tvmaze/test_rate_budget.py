"""The shared TV Maze request budget (ADR-0006).

These are the properties that fail if the bucket ever quietly becomes
per-process again, or if a waiter starts holding the row lock while it sleeps.
"""

import asyncio
import time

import httpx
import pytest
import respx
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tvbf.tvmaze.client import RateLimiter, TVMazeClient
from tvbf.tvmaze.rate_budget import DatabaseRateLimiter


@pytest.fixture
def factory(test_engine):
    return async_sessionmaker(test_engine, expire_on_commit=False)


@pytest.fixture
async def empty_bucket(session):
    """Start each test from no bucket row, so capacity is deterministic.

    `session` is requested for its truncating teardown, which is also what stops
    a bucket drained here from leaking into the next test.
    """
    await session.execute(text("DELETE FROM tvmaze.rate_budget"))
    await session.commit()
    return session


async def test_two_limiters_share_one_budget(factory, empty_bucket):
    """Two limiter instances stand in for two processes.

    Four acquisitions against a capacity-2 bucket: two are free, and the rest
    have to wait out the refill. If each instance kept its own bucket — which is
    what a per-process limiter does — all four would return immediately.
    """
    first = DatabaseRateLimiter(2, 1.0, session_factory=factory)
    second = DatabaseRateLimiter(2, 1.0, session_factory=factory)

    start = time.monotonic()
    await asyncio.gather(first.acquire(), first.acquire(), second.acquire(), second.acquire())
    elapsed = time.monotonic() - start

    # Tokens 3 and 4 arrive at ~0.5s and ~1.0s at 2/sec.
    assert elapsed >= 0.8, f"four acquisitions took {elapsed:.2f}s — the budget is not shared"


async def test_a_waiting_acquirer_does_not_hold_the_row_lock(factory, empty_bucket, test_engine):
    """The single easiest way to get this wrong.

    Sleeping inside the transaction turns the bucket into a serial queue: every
    other process blocks on the row for the full wait, and lock timeouts start
    surfacing as job failures. `FOR UPDATE NOWAIT` raises immediately if the row
    is locked, so it answers the question without a timing guess.
    """
    limiter = DatabaseRateLimiter(1, 1.0, session_factory=factory)
    await limiter.acquire()  # drains the bucket

    waiting = asyncio.create_task(limiter.acquire())
    await asyncio.sleep(0.2)  # long enough that it is inside its sleep
    assert not waiting.done()

    async with test_engine.connect() as conn:
        await conn.execute(
            text("SELECT tokens FROM tvmaze.rate_budget WHERE id = 1 FOR UPDATE NOWAIT")
        )
        await conn.rollback()

    await waiting


async def test_refill_is_time_based_and_capped_at_capacity(factory, empty_bucket):
    limiter = DatabaseRateLimiter(3, 0.3, session_factory=factory)  # 10/sec, capacity 3
    for _ in range(3):
        await limiter.acquire()

    # Idle for a full second: 10 tokens' worth of refill against a cap of 3.
    await asyncio.sleep(1.0)

    start = time.monotonic()
    for _ in range(3):
        await limiter.acquire()
    burst = time.monotonic() - start
    assert burst < 0.1, (
        f"a burst of capacity took {burst:.2f}s — refill is not crediting elapsed time"
    )

    start = time.monotonic()
    await limiter.acquire()
    fourth = time.monotonic() - start
    assert fourth >= 0.05, (
        f"a fourth token came free after {fourth:.2f}s — refill is not capped at capacity"
    )


async def test_an_unreachable_bucket_raises_rather_than_proceeding(empty_bucket):
    """Fail closed. Proceeding unthrottled is the harm this exists to prevent."""
    dead = create_async_engine("postgresql+asyncpg://root:root@127.0.0.1:1/nothing")
    limiter = DatabaseRateLimiter(18, 10.0, session_factory=async_sessionmaker(dead))
    try:
        with pytest.raises((OSError, DBAPIError)):
            await limiter.acquire()
    finally:
        await dead.dispose()


@respx.mock
async def test_an_injected_limiter_bypasses_the_bucket_entirely(empty_bucket):
    """`limiter=` is what keeps the unit suite off a database. Keep it working."""
    respx.get("https://api.tvmaze.com/shows/1").mock(
        return_value=httpx.Response(200, json={"id": 1, "name": "Alpha"})
    )
    async with TVMazeClient(
        base_url="https://api.tvmaze.com",
        rate_calls=2,
        rate_window=1,
        limiter=RateLimiter(calls=2, window_seconds=1),
    ) as client:
        await client.get_show(1)

    rows = (
        await empty_bucket.execute(text("SELECT COUNT(*) FROM tvmaze.rate_budget"))
    ).scalar_one()
    assert rows == 0, "an injected limiter touched the shared bucket"
