# NEU-1008 — Run the daily update as a CLI entrypoint on a Coolify schedule

**Ticket:** [NEU-1008](https://linear.app/neuroticsasquatch/issue/NEU-1008/backend-run-the-daily-update-as-a-cli-entrypoint-on-a-coolify-schedule)
**Related:** [NEU-955](https://linear.app/neuroticsasquatch/issue/NEU-955) (process-wide rate limiter), [NEU-957](https://linear.app/neuroticsasquatch/issue/NEU-957) (divergent-budget warning), [NEU-966](https://linear.app/neuroticsasquatch/issue/NEU-966) (in-flight guard)
**Repo:** `tvbf-backend`

## Problem

`.github/workflows/daily-update.yml` POSTs `https://api.tvbingefriend.com/admin/update` with a `TVBF_ADMIN_TOKEN` repo secret, then polls `/admin/ingest/{run_id}` every 30s for up to 180 minutes — because the route returns `202` and the work runs as an `asyncio` task inside the app. The polling loop exists solely to recover a result the HTTP call threw away.

A CLI entrypoint collapses that: the process *is* the run, so its exit code is the result. No 202, no polling, no admin token leaving the host, no public admin call, and no dependency on GitHub Actions — which was unavailable twice on 2026-08-06.

`upcoming-movies-backend` already runs exactly this shape — `python -m upmovies.pipeline_run` on a Coolify schedule with healthchecks.io deadman pings. Follow it.

## The thing that makes this more than a port

`get_rate_limiter` is `@cache`d and **process-wide** (NEU-955; `CLAUDE.md` records it as load-bearing). `RateLimiter` is a sliding window over a `deque` of `time.monotonic()` timestamps guarded by an `asyncio.Lock` — every one of those is per-process by construction.

A Coolify scheduled task is a **separate process**. The existing guard does not help: `find_live_run` is scoped **per kind**, deliberately, so a stuck backfill never blocks an urgent daily. A CLI `update` and an in-app `episode_credits_backfill` would each pass their own guard and each pace at 18 req/10s — **36 req/10s against TV Maze**, the NEU-955 bug reintroduced architecturally rather than in code.

**Decision: share the budget across processes.** The daily and any in-app job run concurrently and correctly, each simply slower — restoring exactly the property NEU-955 established, now across process boundaries. The rejected alternative was having the CLI stand down whenever any run was live; it was cheaper, but it cost a day's dailies during every long pass and made "is the catalogue updating?" ambiguous.

## Decisions

### 1. A Postgres-backed token bucket replaces the in-process limiter

Not a sliding window. Replicating the current design cross-process means a row per request — ~188k inserts and deletes over a 29-hour backfill, plus the vacuum churn. A token bucket is one row that never grows.

Semantics match today's behaviour: capacity = `calls` (so a burst of 18 is still allowed after idle), refill = `calls / window_seconds` per second.

```
tvmaze.rate_budget
  id          smallint primary key   -- single row, always 1
  tokens      double precision
  updated_at  timestamptz
```

Acquire:

1. `BEGIN`; `SELECT … FOR UPDATE` — the row lock is what serializes processes
2. Refill by `elapsed × rate`, capped at capacity, using **`clock_timestamp()`**
3. Token available → decrement, `COMMIT`, proceed
4. None → compute the wait, **`COMMIT` (releasing the lock)**, sleep, retry from 1

**Never sleep holding the lock.** Holding it across a sleep turns the bucket into a serial queue: every other process blocks on the row for the full wait, and lock timeouts start surfacing as job failures. This is the single easiest way to get this wrong.

**Use database time, not `time.monotonic()`.** Monotonic clocks are per-process and not comparable between them — the whole reason the current limiter cannot be shared. `clock_timestamp()` rather than `now()`, since `now()` is transaction-start time and would drift under contention.

### 2. When the bucket is unreachable, the request fails

After the client's normal retries, a bucket query that still fails raises rather than proceeding unthrottled. Proceeding is precisely the harm this exists to prevent, and a job that fails loudly is recoverable where an unthrottled one risks the upstream relationship. The per-show failure paths already treat this correctly — non-fatal, counted, and abort-on-consecutive.

### 3. `client.py` gains a database dependency, deliberately

`client.py` has no DB knowledge today. The limiter needs a session factory, so either it moves to its own module that imports `db`, or the factory is injected into `TVMazeClient`.

**Prefer injection.** `TVMazeClient` is duck-typed by several callers and constructed in tests with `limiter=` for isolation (`CLAUDE.md` documents that escape hatch). Keep `limiter=` working: the parameter takes anything with `async acquire()`, so tests keep using an in-memory limiter and never touch the DB. That is also what keeps the unit tests fast.

### 4. The guard stays per-kind everywhere

With a shared budget there is no cross-process hazard left, so the CLI uses the same per-kind `find_live_run` check as the route. No asymmetry, nothing to explain.

### 5. `POST /admin/update` survives

Kept as the manual trigger — the CLI cannot be invoked from outside the host, and an ad-hoc daily is useful. Both paths share `_background_update`, so behaviour cannot drift; only `await` vs `create_task` differs.

### 6. Healthchecks deadman, following `upmovies`

Coolify notifies on a **failed** scheduled task but cannot notify that the task **never ran** — suspended and forgotten, container down, scheduler broken. That gap is the reason for a deadman.

```python
healthcheck_daily_url: str | None = Field(default=None, alias="HEALTHCHECK_DAILY_URL")
```

Unset → every ping is a no-op, so local runs and tests never call out. Wired through `docker-compose.prod.yml` as `${HEALTHCHECK_DAILY_URL:-}`.

| moment | ping |
| --- | --- |
| start of the process | `/start` |
| run succeeded | base URL |
| run failed or cancelled | `/fail` |

No skip case — with a shared budget the daily always runs.

Pings are **best-effort**: `try/except`, logged and swallowed, never affecting the exit code. A ping that cannot reach healthchecks is itself the signal healthchecks alerts on.

## What to build

### 1. The shared limiter

New module (`tvmaze/rate_budget.py`) plus an Alembic migration creating `tvmaze.rate_budget` and seeding its single row. `get_rate_limiter`'s `@cache` and its NEU-957 divergent-budget warning still apply per process — and matter *more* now, because two processes reading different `TVMAZE_RATE_LIMIT_*` values would size the same shared bucket differently. Keep the warning.

### 2. `src/tvbf/jobs/daily_update.py`

A `jobs` package, so `people:update` and future scheduled jobs have an obvious home. Invoked as `python -m tvbf.jobs.daily_update`, matching `python -m upmovies.pipeline_run`.

1. `/start` ping
2. Per-kind `find_live_run` → live → log and exit 0 (an operator already triggered one; nothing to add)
3. `create_run(kind="update")`, commit
4. **`await`** the same body `_background_update` runs — not `create_task`
5. Re-read the run row: `succeeded` → success ping, exit 0; `failed`/`cancelled` → `/fail` ping, exit 1

Exit codes are the contract Coolify reads: **0 = the daily ran and succeeded; 1 = it failed.**

### 3. Refactor, minimally

`_start_run` (`routers/admin.py:36`) decomposes into guard → create → spawn; only the `HTTPException` translation is route-coupled, and `find_live_run` is already in `runs.py`. Separate the check from its HTTP wrapper and make `_background_update` importable by both. Nothing more.

### 4. Delete the workflow

- `.github/workflows/daily-update.yml`
- The `TVBF_ADMIN_TOKEN` repo secret — a human action in GitHub settings; note it in the PR

### 5. Rewrite the suspend/resume instructions

They are **not** deleted. With a shared budget, running the daily during a long pass is *correct* — both jobs simply go slower, exactly as `CLAUDE.md` already describes for concurrent in-process jobs. Pausing the daily remains a legitimate **efficiency** choice when you want a pass to have the full budget.

What changes is the mechanism: `gh workflow disable "Daily TV Maze update"` no longer exists, and suspending a Coolify scheduled task is a UI action.

| file | what |
| --- | --- |
| `scripts/verify_episode_credits.sh:439` | prints "Re-enable the daily-update cron" as post-run step 1 |
| `scripts/verify_pass_a.sh:316` | same |
| `docs/specs/NEU-967-prune-deleted-seasons.md:107,113` | runbook steps 2 and 8 |
| `docs/superpowers/plans/2026-08-01-cast-and-crew.md:7` | parenthetical advice |

The two scripts matter most: they **print** their line to whoever runs them, so leaving them sends an operator to a workflow that no longer exists. Reword to "optionally suspend the daily scheduled task in Coolify so this pass has the full request budget", and say plainly that it is optional now.

### 6. Documentation

- **ADR-0006** — *the TV Maze request budget is shared across processes*. Records that the cap belongs to us as a whole, that a per-process limiter cannot express it once jobs run outside the app, and the token-bucket design with the never-sleep-holding-the-lock rule. This is the piece most likely to be undone by someone optimising away a DB round-trip.
- **`CLAUDE.md`** — the entrypoint, and an amendment to the existing "process-wide, not per client" note, which becomes "cross-process, not per process".

## Traps

**Never sleep holding the row lock.** See decision 1.

**Do not fall back to the in-process limiter when the DB is unavailable.** That silently restores the doubled rate under exactly the conditions where nobody is watching.

**`await`, not `create_task`.** The whole value of the CLI is that the process outlives the work and its exit code reflects it. `create_task` would exit 0 immediately every day forever — a silent no-op that looks perfectly healthy, which is worse than today.

**Keep `limiter=` working.** `tests/conftest.py` relies on it for isolation; without it every unit test needs a database.

**The budget must match across processes.** Both read the same `TVMAZE_RATE_LIMIT_*` env; NEU-957's warning is the only thing that would surface a mismatch, and it is per-process, so it cannot see the other side. Note the limitation rather than pretending otherwise.

## Testing

1. Two concurrent acquirers against one bucket take at least the expected wall-clock for the combined budget — the property that fails if the bucket is per-process.
2. A waiter does not hold the row lock while sleeping: a second acquirer can reach the row during the first's wait.
3. Refill is time-based and capped at capacity — after idle, a burst of `calls` proceeds without waiting.
4. A bucket that cannot be reached raises rather than proceeding.
5. `limiter=` still bypasses the bucket entirely, so unit tests need no database.
6. CLI: nothing live → runs and exits 0.
7. CLI: run finalizes `failed` → exits 1 and pings `/fail`.
8. CLI: `HEALTHCHECK_DAILY_URL` unset → no HTTP call attempted.
9. CLI: a ping that raises is logged, swallowed, exit code unchanged.
10. `POST /admin/update` still 409s on a live `update` run.

## Acceptance

- The daily runs on a Coolify schedule with no GitHub Actions involvement and no `TVBF_ADMIN_TOKEN`
- A daily and an in-app backfill run concurrently without exceeding 18 req/10s in total
- A failed run exits non-zero; Coolify notifies, and healthchecks catches a run that never happens
- No script prints a `gh workflow` command
- `.github/workflows/daily-update.yml` deleted
- ADR-0006 written; `CLAUDE.md` amended
- `task lint`, `task typecheck`, `task test` green

## Out of scope

**Moving `people:update`.** Same treatment, and the `jobs` package is shaped for it, but it is a separate job with its own schedule and deadman. Do it after this has run in prod for a week.

**Making the bucket fair.** Multiple waiters retry-poll, so there is no queue and no ordering guarantee. With two processes that is not worth solving; revisit if it ever becomes many.

**The `429` retry loop.** `_request` retries a 429 indefinitely without consuming retry budget — noted in NEU-1006's spec, still unaddressed, unrelated to this.
