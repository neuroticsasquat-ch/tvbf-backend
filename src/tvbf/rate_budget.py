"""Request budgets, shared across processes and keyed by upstream (ADR-0006).

`RateLimiter` paces one process. That was enough while every job ran inside the
app: `get_rate_limiter` handed them all one bucket (NEU-955), so concurrent jobs
split a single budget and simply ran slower.

The daily update now runs as its own process on a Coolify schedule (NEU-1008),
and a per-process limiter cannot see it. Two processes each pacing at the
configured rate is twice the configured rate upstream — the NEU-955 bug back
again, architecturally this time. So the budget lives in Postgres, which is the
one thing both processes already share.

This module used to be `tvbf/tvmaze/rate_budget.py` and served exactly one
upstream. TMDB needs its own ceiling and TV Maze's is untouched by it, so the
bucket is now keyed: a `Bucket` says which row holds which budget, and
`get_rate_limiter(source, ...)` resolves the source name to one (NEU-1027).
"""

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from typing import Protocol

from sqlalchemy import TextClause, text
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.db import SessionLocal

log = logging.getLogger(__name__)

SessionFactory = Callable[[], AsyncSession]


class Limiter(Protocol):
    """Anything that can pace requests. `limiter=` accepts any of them."""

    async def acquire(self, n: int = 1) -> None: ...


class RateLimiter:
    """Sliding-window limiter, per process. Allows `calls` per `window_seconds`.

    No longer the default — `get_rate_limiter` returns a `DatabaseRateLimiter`
    so the budget spans processes (ADR-0006). This survives as the isolated
    limiter tests pass via `limiter=`, which is what keeps the unit suite off
    the database.
    """

    def __init__(self, calls: int, window_seconds: float):
        self._calls = calls
        self._window = window_seconds
        self._timestamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self, n: int = 1) -> None:
        # One slot at a time: this limiter has no lease to amortise a block
        # over, so `n` slots and `n` acquisitions are the same thing.
        for _ in range(n):
            await self._acquire_one()

    async def _acquire_one(self) -> None:
        async with self._lock:
            now = time.monotonic()
            while self._timestamps and now - self._timestamps[0] >= self._window:
                self._timestamps.popleft()
            if len(self._timestamps) >= self._calls:
                wait = self._window - (now - self._timestamps[0])
                if wait > 0:
                    await asyncio.sleep(wait)
                    now = time.monotonic()
                    while self._timestamps and now - self._timestamps[0] >= self._window:
                        self._timestamps.popleft()
            self._timestamps.append(time.monotonic())


@dataclass(frozen=True)
class Bucket:
    """Which row holds one upstream's budget.

    The table and the key column are both parameters because TV Maze's bucket
    predates the keyed design and stays exactly where it is: `catalog` is where
    every *new* budget lives, but migrating a live token bucket mid-ingest buys
    nothing, and both upstreams run side by side until cutover (NEU-1027).

    `table` and `key_column` are interpolated into SQL, so they must never come
    from anywhere but the module-level registry below.
    """

    table: str
    key_column: str
    key: str | int


# `catalog.rate_budget` is keyed by source name; `tvmaze.rate_budget` is the
# original single-row bucket, keyed by the constant id its check constraint
# pins to 1.
TVMAZE_BUCKET = Bucket(table="tvmaze.rate_budget", key_column="id", key=1)
TMDB_BUCKET = Bucket(table="catalog.rate_budget", key_column="source", key="tmdb")

BUCKETS: dict[str, Bucket] = {"tvmaze": TVMAZE_BUCKET, "tmdb": TMDB_BUCKET}


@dataclass(frozen=True, order=True)
class Budget:
    """How fast one source may be called, and how much is taken per lock.

    A value object rather than three loose arguments because `get_rate_limiter`
    is `@cache`d on it, and `functools.cache` keys on the literal call — it does
    not bind defaults or normalise keywords, so `(20, 1.0)`, `(20, 1.0, 25)` and
    `(20, 1.0, lease=25)` would otherwise be three cache entries, i.e. three
    limiters holding three simultaneous leases against one budget row. Dataclass
    equality collapses all three call forms to one key.
    """

    calls: int
    window_seconds: float
    # Tokens taken per locked transaction. 1 is the pre-NEU-1027 behaviour token
    # for token, which is what leaves TV Maze's calibration untouched.
    lease: int = 1

    def __post_init__(self) -> None:
        if self.lease < 1:
            raise ValueError("lease must be at least 1 token")

    @property
    def capacity(self) -> float:
        return float(self.calls)

    @property
    def rate(self) -> float:
        """Tokens refilled per second."""
        return self.calls / self.window_seconds


@cache
def _statements(bucket: Bucket) -> tuple[TextClause, TextClause, TextClause]:
    """The lock/seed/take statements for one bucket.

    `clock_timestamp()` rather than `now()`: `now()` is transaction-start time,
    so under contention a transaction that waited on the row lock would measure
    elapsed time from before it waited and over-refill.
    """
    return (
        text(
            f"SELECT tokens, EXTRACT(EPOCH FROM (clock_timestamp() - updated_at)) AS elapsed "
            f"FROM {bucket.table} WHERE {bucket.key_column} = :key FOR UPDATE"
        ),
        text(
            f"INSERT INTO {bucket.table} ({bucket.key_column}, tokens, updated_at) "
            f"VALUES (:key, :tokens, clock_timestamp()) "
            f"ON CONFLICT ({bucket.key_column}) DO NOTHING"
        ),
        text(
            f"UPDATE {bucket.table} SET tokens = :tokens, updated_at = clock_timestamp() "
            f"WHERE {bucket.key_column} = :key"
        ),
    )


class DatabaseRateLimiter:
    """Token bucket in Postgres. Interchangeable with `RateLimiter`.

    A token bucket, not a sliding window. Porting the window's design would mean
    a row per request — roughly 188k inserts and deletes over a 29-hour backfill,
    plus the vacuum churn behind them. A bucket is one row that never grows, and
    the semantics match: capacity is `calls`, so a burst of `calls` is still
    allowed after idle, and refill is `calls / window_seconds` per second.

    **Never sleeps holding the row lock.** A waiter commits, releases, sleeps
    outside the transaction, and retries. Holding the lock across the sleep would
    turn the bucket into a serial queue in which every other process blocks for
    the full wait, and lock timeouts would start surfacing as job failures.

    **Fails closed.** A bucket that cannot be reached raises, and the caller's
    per-entity failure handling counts it. Falling back to an in-process limiter
    would silently restore the doubled rate under exactly the conditions where
    nobody is watching.

    **Leases blocks of `lease` tokens.** At TV Maze's 1.8 req/s a locked round
    trip per request is free; at TMDB's 20 req/s it is 20 serialised
    transactions per second through one row, roughly 20× the pressure this
    design was validated at, on the component whose failure mode is lock
    timeouts surfacing as job failures. Leasing ~25 tokens per locked
    transaction cuts lock traffic to ~1/s and preserves the cross-process
    guarantee exactly, because the block is deducted atomically before any of it
    is spent. A process that dies mid-lease forfeits the unspent remainder,
    which errs slow — the direction `_take` already deliberately errs.

    `Budget.lease` of 1, the default, is the pre-NEU-1027 behaviour token for
    token, which is what leaves TV Maze's calibration untouched.
    """

    def __init__(
        self,
        bucket: Bucket,
        budget: Budget,
        *,
        session_factory: SessionFactory | None = None,
    ):
        self._bucket = bucket
        self._capacity = budget.capacity
        self._rate = budget.rate
        self._lease_size = budget.lease
        self._session_factory = session_factory or SessionLocal
        # Tokens taken from the shared bucket and not yet spent. Held per
        # instance, so `get_rate_limiter`'s caching is what keeps a process from
        # holding several leases against one budget at once.
        self._leased = 0
        self._lock = asyncio.Lock()

    async def acquire(self, n: int = 1) -> None:
        """Spend `n` tokens, waiting on the shared budget for as long as it takes.

        Serialised in-process, and the lock is held across the wait. That is a
        local lock, not the row lock — the never-sleep-holding-the-row-lock rule
        is about the transaction, and this sleeps well outside it. Callers of
        one limiter are all contending for one budget, so queueing them costs no
        throughput and is what keeps `_leased` honest; the cost is that a
        waiter also blocks a caller the leased remainder could have served,
        which errs slow.
        """
        if n < 1:
            raise ValueError("n must be at least 1 token")
        async with self._lock:
            while self._leased < n:
                want = max(n, self._lease_size) - self._leased
                self._leased += await self._lease(want)
            self._leased -= n

    async def _lease(self, want: int) -> int:
        """Take up to `want` tokens from the bucket, waiting until at least one is free."""
        while True:
            granted, wait = await self._take(want)
            if granted:
                return granted
            await asyncio.sleep(wait)

    async def _take(self, want: int) -> tuple[int, float]:
        """Take up to `want` whole tokens, or report the seconds to wait for one.

        The transaction spans the lock, the refill and the decrement, and nothing
        else — see the never-sleep-holding-the-lock rule above.

        A short grant is deliberate: a caller wanting a block of 25 against a
        bucket holding 3 takes the 3 and comes back, rather than waiting for the
        whole block while other processes drain what it could have used.
        """
        lock, seed, take = _statements(self._bucket)
        key = self._bucket.key
        async with self._session_factory() as s:
            async with s.begin():
                row = (await s.execute(lock, {"key": key})).first()
                if row is None:
                    # Migrations seed the row; this covers a database built from
                    # `Base.metadata.create_all` (tests) and makes the limiter
                    # self-healing if the row is ever truncated away.
                    await s.execute(seed, {"key": key, "tokens": self._capacity})
                    row = (await s.execute(lock, {"key": key})).one()

                tokens = min(self._capacity, float(row.tokens) + float(row.elapsed) * self._rate)
                granted = min(want, int(tokens))
                if granted >= 1:
                    # `take` re-reads `clock_timestamp()`, so the microseconds
                    # between the two statements are dropped rather than banked.
                    # That errs slow, which is the safe direction.
                    await s.execute(take, {"key": key, "tokens": tokens - granted})
                    return granted, 0.0

                # Deliberately writes nothing. `updated_at` stays put, so the
                # elapsed time this attempt saw is still credited to whoever
                # succeeds next.
                return 0, (1.0 - tokens) / self._rate


def build_limiter(bucket: Bucket, budget: Budget) -> Limiter:
    """Build the limiter behind one budget.

    A seam, not indirection for its own sake: `tests/conftest.py` swaps this
    function for one returning the in-process `RateLimiter`, which is what keeps
    ~50 client constructions across the unit suite off a database. The shared
    bucket has its own integration tests.
    """
    return DatabaseRateLimiter(bucket, budget)


# Every budget `get_rate_limiter` has been asked for, per source. Tracked
# separately from the cache because `cache_info()` reports a size, not the keys
# behind it.
_seen_budgets: dict[str, set[Budget]] = {}


@cache
def get_rate_limiter(source: str, budget: Budget) -> Limiter:
    """The limiter for one upstream's request budget, shared by every process.

    An upstream's cap applies to us as a whole, not to each job. Every admin
    route builds its own client, so a per-instance limiter let two concurrent
    jobs each pace at the configured rate and hit upstream at twice it — over
    the cap, with neither throttling the other. Sharing one bucket means
    concurrent jobs split a single budget and simply run slower, which is the
    intended behaviour.

    The bucket lives in Postgres, so that holds across processes too — which it
    has to, now the daily update runs as its own process rather than as a task
    inside the app (ADR-0006). Caching stays worthwhile even so: the instance is
    cheap, but the cache is what makes a divergent budget detectable at all, and
    with leasing it is also what stops one process holding several leases
    against the same budget.

    Cached rather than built at import so the settings that size it are read
    when the first client is constructed. Tests reset it through
    `reset_rate_limiters()`, which `tests/conftest.py` calls between tests. Do
    not call `get_rate_limiter.cache_clear()` directly — it leaves
    `_seen_budgets` populated, so the cache and the seen set fall out of step.

    The cache is keyed by source *and* budget, so callers asking for different
    numbers against one source get different buckets — which would reintroduce
    exactly the overshoot this exists to prevent. Every construction site reads
    the same `Settings`, so it cannot happen today; a second budget warns rather
    than failing silently, because the symptom otherwise is invisible (NEU-957).
    Size a new caller from settings too. Two *sources* on different numbers are
    the normal case and say nothing.

    The warning is per process, so it catches a divergence *within* one and not
    between two. Two processes reading different rate-limit env values would
    size the same shared bucket differently and neither would say so — a real
    limitation, and the reason both read the same env.
    """
    bucket = BUCKETS.get(source)
    if bucket is None:
        raise KeyError(f"no rate budget registered for source {source!r}")

    # The membership half only matters if someone bypassed `reset_rate_limiters`
    # and cleared the cache alone: the budget would then be a miss while still
    # in `_seen_budgets`, and warning about it would be noise.
    seen = _seen_budgets.setdefault(source, set())
    if seen and budget not in seen:
        log.warning(
            "additional %s rate budget requested (%s per %ss, lease %s; already have %s) — "
            "jobs on different budgets no longer share one limiter and will "
            "exceed the upstream cap together",
            source,
            budget.calls,
            budget.window_seconds,
            budget.lease,
            sorted(seen),
        )
    seen.add(budget)
    # Looked up as a module global on purpose: `tests/conftest.py` swaps this
    # name so no unit test needs a database.
    return build_limiter(bucket, budget)


def reset_rate_limiters() -> None:
    """Drop the cached limiters and the budgets seen so far.

    For tests only — `tests/conftest.py` calls this between tests so the
    divergence warning does not leak into the next one.
    """
    get_rate_limiter.cache_clear()
    _seen_budgets.clear()
