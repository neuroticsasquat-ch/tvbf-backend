"""The shared request budgets (ADR-0006, NEU-1027).

These are the properties that fail if a bucket ever quietly becomes per-process
again, if a waiter starts holding the row lock while it sleeps, or if a leased
block is handed out before it has been deducted.
"""

import asyncio
import time

import httpx
import pytest
import respx
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tvbf.rate_budget import (
    BUCKETS,
    TMDB_BUCKET,
    Bucket,
    Budget,
    DatabaseRateLimiter,
    RateLimiter,
)
from tvbf.tmdb.client import TMDBClient

# Every registered bucket, so a new upstream cannot be added without these
# properties being asserted for it too.
ALL_BUCKETS = pytest.mark.parametrize("bucket", BUCKETS.values(), ids=list(BUCKETS))


@pytest.fixture
def factory(test_engine):
    return async_sessionmaker(test_engine, expire_on_commit=False)


@pytest.fixture
async def empty_buckets(session):
    """Start each test from no bucket rows, so capacity is deterministic.

    `session` is requested for its truncating teardown, which is also what stops
    a bucket drained here from leaking into the next test.
    """
    for bucket in BUCKETS.values():
        await session.execute(text(f"DELETE FROM {bucket.table}"))
    await session.commit()
    return session


async def _tokens(session, bucket: Bucket) -> float:
    return (
        await session.execute(
            text(f"SELECT tokens FROM {bucket.table} WHERE {bucket.key_column} = :key"),
            {"key": bucket.key},
        )
    ).scalar_one()


@ALL_BUCKETS
async def test_two_limiters_share_one_budget(bucket, factory, empty_buckets):
    """Two limiter instances stand in for two processes.

    Four acquisitions against a capacity-2 bucket: two are free, and the rest
    have to wait out the refill. If each instance kept its own bucket — which is
    what a per-process limiter does — all four would return immediately.
    """
    first = DatabaseRateLimiter(bucket, Budget(2, 1.0), session_factory=factory)
    second = DatabaseRateLimiter(bucket, Budget(2, 1.0), session_factory=factory)

    start = time.monotonic()
    await asyncio.gather(first.acquire(), first.acquire(), second.acquire(), second.acquire())
    elapsed = time.monotonic() - start

    # Tokens 3 and 4 arrive at ~0.5s and ~1.0s at 2/sec.
    assert elapsed >= 0.8, f"four acquisitions took {elapsed:.2f}s — the budget is not shared"


async def test_separate_sources_do_not_share_a_budget(factory, empty_buckets):
    """Each upstream's ceiling is its own.

    TMDB is the only registered source since NEU-1050 retired TV Maze, so the
    second source here is a bucket built for this test — the property has to
    keep holding for whichever upstream is added next, and a registry of one
    cannot assert it. A bucket keyed by source that nonetheless throttled every
    source together would silently cost the newcomer its rate.
    """
    other = Bucket(table="catalog.rate_budget", key_column="source", key="other")

    tmdb = DatabaseRateLimiter(TMDB_BUCKET, Budget(1, 10.0), session_factory=factory)
    await tmdb.acquire()  # drains the TMDB bucket for the next 10 seconds

    second = DatabaseRateLimiter(other, Budget(1, 10.0), session_factory=factory)
    start = time.monotonic()
    await second.acquire()
    elapsed = time.monotonic() - start

    assert elapsed < 0.5, f"the second source waited {elapsed:.2f}s — the buckets are shared"


@ALL_BUCKETS
async def test_a_waiting_acquirer_does_not_hold_the_row_lock(
    bucket, factory, empty_buckets, test_engine
):
    """The single easiest way to get this wrong.

    Sleeping inside the transaction turns the bucket into a serial queue: every
    other process blocks on the row for the full wait, and lock timeouts start
    surfacing as job failures. `FOR UPDATE NOWAIT` raises immediately if the row
    is locked, so it answers the question without a timing guess.
    """
    limiter = DatabaseRateLimiter(bucket, Budget(1, 1.0), session_factory=factory)
    await limiter.acquire()  # drains the bucket

    waiting = asyncio.create_task(limiter.acquire())
    await asyncio.sleep(0.2)  # long enough that it is inside its sleep
    assert not waiting.done()

    async with test_engine.connect() as conn:
        await conn.execute(
            text(
                f"SELECT tokens FROM {bucket.table} "
                f"WHERE {bucket.key_column} = :key FOR UPDATE NOWAIT"
            ),
            {"key": bucket.key},
        )
        await conn.rollback()

    await waiting


@ALL_BUCKETS
async def test_refill_is_time_based_and_capped_at_capacity(bucket, factory, empty_buckets):
    # 10/sec, capacity 3
    limiter = DatabaseRateLimiter(bucket, Budget(3, 0.3), session_factory=factory)
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


async def test_a_lease_is_deducted_before_any_of_it_is_spent(factory, empty_buckets):
    """The whole basis of the cross-process guarantee under leasing.

    One locked transaction deducts the block; the tokens are then spent locally
    with no further lock traffic, which is what takes ~20 serialised
    transactions per second down to ~1 at TMDB's rate. If the block were
    deducted as it was spent instead, another process would see tokens that are
    already committed elsewhere.
    """
    limiter = DatabaseRateLimiter(TMDB_BUCKET, Budget(10, 10.0, lease=5), session_factory=factory)

    await limiter.acquire()
    assert await _tokens(empty_buckets, TMDB_BUCKET) == 5.0, (
        "the first acquisition did not deduct the whole block"
    )

    for _ in range(4):
        await limiter.acquire()
    assert await _tokens(empty_buckets, TMDB_BUCKET) == 5.0, (
        "spending the leased block went back to the row — the lease is not being held"
    )


async def test_an_abandoned_lease_does_not_over_issue(factory, empty_buckets):
    """A process dying mid-lease forfeits the unspent remainder.

    That errs slow, which is the safe direction: the alternative is a block that
    is spendable twice, once by the process that leased it and once by whoever
    reads a row that never lost it.

    `doomed` stands in for the dead process: nothing releases its remainder,
    because there is no release path to call. `survivor` is what any other
    process would see, and what it sees is a bucket four tokens lighter.
    """
    doomed = DatabaseRateLimiter(  # 1/sec
        TMDB_BUCKET, Budget(6, 6.0, lease=5), session_factory=factory
    )
    await doomed.acquire()  # leases 5, spends 1, holds 4 it will never spend

    survivor = DatabaseRateLimiter(TMDB_BUCKET, Budget(6, 6.0), session_factory=factory)
    assert await _tokens(empty_buckets, TMDB_BUCKET) == 1.0, (
        "the abandoned block is still in the row — it was never deducted"
    )
    await survivor.acquire()  # the single token the abandoned lease left behind

    start = time.monotonic()
    await survivor.acquire()
    elapsed = time.monotonic() - start
    assert elapsed >= 0.8, (
        f"a token came free after {elapsed:.2f}s — the forfeited block was re-issued"
    )


async def test_a_block_larger_than_the_bucket_holds_is_granted_short(factory, empty_buckets):
    """A caller wanting more than is there takes what is there and comes back.

    Waiting for the whole block under the lock-free retry loop would let other
    processes drain exactly what this one could have used, and a lease larger
    than capacity would never be grantable at all.
    """
    limiter = DatabaseRateLimiter(TMDB_BUCKET, Budget(3, 0.3, lease=25), session_factory=factory)

    start = time.monotonic()
    await limiter.acquire()
    elapsed = time.monotonic() - start

    assert elapsed < 0.2, f"a lease of 25 against a capacity-3 bucket waited {elapsed:.2f}s"
    assert await _tokens(empty_buckets, TMDB_BUCKET) == 0.0


@ALL_BUCKETS
async def test_an_unreachable_bucket_raises_rather_than_proceeding(bucket, empty_buckets):
    """Fail closed. Proceeding unthrottled is the harm this exists to prevent."""
    dead = create_async_engine("postgresql+asyncpg://root:root@127.0.0.1:1/nothing")
    limiter = DatabaseRateLimiter(
        bucket, Budget(18, 10.0), session_factory=async_sessionmaker(dead)
    )
    try:
        with pytest.raises((OSError, DBAPIError)):
            await limiter.acquire()
    finally:
        await dead.dispose()


@respx.mock
async def test_an_injected_limiter_bypasses_the_bucket_entirely(empty_buckets):
    """`limiter=` is what keeps the unit suite off a database. Keep it working."""
    respx.get("https://api.themoviedb.org/3/tv/1").mock(
        return_value=httpx.Response(200, json={"id": 1, "name": "Alpha"})
    )
    async with TMDBClient(
        base_url="https://api.themoviedb.org/3",
        read_access_token="token",
        rate_calls=2,
        rate_window=1,
        limiter=RateLimiter(calls=2, window_seconds=1),
    ) as client:
        await client.get_tv_series(1, append=[])

    rows = (
        await empty_buckets.execute(text(f"SELECT COUNT(*) FROM {TMDB_BUCKET.table}"))
    ).scalar_one()
    assert rows == 0, "an injected limiter touched the shared bucket"
