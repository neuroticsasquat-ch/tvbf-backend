"""The TV Maze request budget, shared across processes (ADR-0006).

`RateLimiter` in `client.py` paces one process. That was enough while every job
ran inside the app: `get_rate_limiter` handed them all one bucket (NEU-955), so
concurrent jobs split a single 18 req/10s budget and simply ran slower.

The daily update now runs as its own process on a Coolify schedule (NEU-1008),
and a per-process limiter cannot see it. Two processes each pacing at the
configured rate is 36 req/10s against TV Maze — the NEU-955 bug back again,
architecturally this time. So the budget moves into Postgres, which is the one
thing both processes already share.
"""

import asyncio
import logging
from collections.abc import Callable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.db import SessionLocal

log = logging.getLogger(__name__)

SessionFactory = Callable[[], AsyncSession]

# The bucket is a single row and its id is a constant, not a parameter. A second
# row would be a second budget.
_ROW_ID = 1

# `clock_timestamp()` rather than `now()`: `now()` is transaction-start time, so
# under contention a transaction that waited on the row lock would measure
# elapsed time from before it waited and over-refill.
_LOCK = text(
    "SELECT tokens, EXTRACT(EPOCH FROM (clock_timestamp() - updated_at)) AS elapsed "
    "FROM tvmaze.rate_budget WHERE id = :id FOR UPDATE"
)
_SEED = text(
    "INSERT INTO tvmaze.rate_budget (id, tokens, updated_at) "
    "VALUES (:id, :tokens, clock_timestamp()) ON CONFLICT (id) DO NOTHING"
)
_TAKE = text(
    "UPDATE tvmaze.rate_budget SET tokens = :tokens, updated_at = clock_timestamp() WHERE id = :id"
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
    """

    def __init__(
        self,
        calls: int,
        window_seconds: float,
        *,
        session_factory: SessionFactory | None = None,
    ):
        self._capacity = float(calls)
        self._rate = calls / window_seconds
        self._session_factory = session_factory or SessionLocal

    async def acquire(self) -> None:
        while True:
            wait = await self._take()
            if wait is None:
                return
            await asyncio.sleep(wait)

    async def _take(self) -> float | None:
        """Take a token, or return the seconds to wait before trying again.

        The transaction spans the lock, the refill and the decrement, and nothing
        else — see the never-sleep-holding-the-lock rule above.
        """
        async with self._session_factory() as s:
            async with s.begin():
                row = (await s.execute(_LOCK, {"id": _ROW_ID})).first()
                if row is None:
                    # Migrations seed the row; this covers a database built from
                    # `Base.metadata.create_all` (tests) and makes the limiter
                    # self-healing if the row is ever truncated away.
                    await s.execute(_SEED, {"id": _ROW_ID, "tokens": self._capacity})
                    row = (await s.execute(_LOCK, {"id": _ROW_ID})).one()

                tokens = min(self._capacity, float(row.tokens) + float(row.elapsed) * self._rate)
                if tokens >= 1.0:
                    # `_TAKE` re-reads `clock_timestamp()`, so the microseconds
                    # between the two statements are dropped rather than banked.
                    # That errs slow, which is the safe direction.
                    await s.execute(_TAKE, {"id": _ROW_ID, "tokens": tokens - 1.0})
                    return None

                # Deliberately writes nothing. `updated_at` stays put, so the
                # elapsed time this attempt saw is still credited to whoever
                # succeeds next.
                return (1.0 - tokens) / self._rate
