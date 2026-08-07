# ADR-0006: The TV Maze request budget is shared across processes

**Status:** accepted (NEU-1008)
**Amends:** the process-wide limiter established by NEU-955

## Context

TV Maze's rate limit applies to *us*, not to each job we happen to run. NEU-955
established that: `TVMazeClient` used to build its own `RateLimiter`, so two
concurrent admin jobs each paced at 18 req/10s and hit upstream at 36. The fix
was `get_rate_limiter`, a `@cache`d accessor handing every client one bucket.

That worked because of an invariant nobody wrote down: **every job ran inside
the app process.** `RateLimiter` is a sliding window over a `deque` of
`time.monotonic()` timestamps guarded by an `asyncio.Lock` — every one of those
is per-process by construction.

NEU-1008 breaks the invariant. The daily update moves off GitHub Actions onto a
Coolify scheduled task invoked as `python -m tvbf.jobs.daily_update`, which is a
separate process. The existing DB guard does not help: `find_live_run` is scoped
per kind, deliberately, so a stuck backfill never blocks an urgent daily. A CLI
`update` and an in-app `episode_credits_backfill` each pass their own guard and
each pace at the full rate — the NEU-955 bug back again, architecturally rather
than in code, and with nothing in place to detect it.

The alternative considered was having the CLI stand down whenever any run was
live. Cheaper, but it cost a day's dailies during every long pass — and long
passes are measured in tens of hours — and it made "is the catalogue updating?"
ambiguous in exactly the window where you most want a straight answer.

## Decision

**The budget lives in Postgres, as a token bucket in `tvmaze.rate_budget`.** One
row. Capacity is `TVMAZE_RATE_LIMIT_REQUESTS`; refill is
`requests / window_seconds` per second. `DatabaseRateLimiter` is what
`get_rate_limiter` now returns.

Acquiring a token is: `BEGIN`, `SELECT … FOR UPDATE`, refill by
`elapsed × rate` capped at capacity, then either decrement and commit, or commit
and wait.

Three properties are load-bearing.

**Never sleep holding the row lock.** A waiter commits, releases, sleeps outside
the transaction, and retries. Holding it across the sleep turns the bucket into a
serial queue: every other process blocks on the row for the full wait, and lock
timeouts start surfacing as job failures.
`test_a_waiting_acquirer_does_not_hold_the_row_lock` pins this with
`FOR UPDATE NOWAIT`.

**Database time, not `time.monotonic()`.** Monotonic clocks are per-process and
not comparable between them — that is precisely why the old limiter could not be
shared. `clock_timestamp()` rather than `now()`, because `now()` is
transaction-start time, so a transaction that waited on the lock would measure
elapsed time from before it waited and over-refill.

**Fail closed.** A bucket that cannot be reached raises. The caller's per-entity
failure handling counts it, non-fatally, and the consecutive-failure abort
catches a sustained outage. Falling back to an in-process limiter would silently
restore the doubled rate under exactly the conditions where nobody is watching.

## Consequences

Every upstream request now costs a database round-trip. At 18 req/10s that is
nothing next to the pacing itself, and it is the price of the cap meaning
anything once jobs run outside the app. **Do not optimise it away.**

A token bucket, not a sliding window. Replicating the window cross-process means
a row per request — ~188k inserts and deletes over a 29-hour backfill, plus the
vacuum churn. The bucket is one row that never grows, and the semantics that
matter are the same: a burst of `calls` is still allowed after idle.

Concurrent jobs are now correct rather than merely tolerated, which is why the
CLI keeps the same per-kind guard as the route and why the long-pass runbooks
now describe suspending the daily as an *efficiency* choice rather than a
correctness one.

Multiple waiters retry-poll, so there is no queue and no ordering guarantee.
With two processes that is not worth solving.

`get_rate_limiter` stays `@cache`d and keeps its divergent-budget warning
(NEU-957). It matters more now, not less: two processes reading different
`TVMAZE_RATE_LIMIT_*` values would size the same shared bucket differently. The
warning is still per-process, so it cannot see the other side — it catches a
divergence within one process, not between two.

`limiter=` remains the escape hatch, and `tests/conftest.py` swaps the in-process
`RateLimiter` back in for the whole suite. Without that, every unit test that
builds a `TVMazeClient` would need a database.
