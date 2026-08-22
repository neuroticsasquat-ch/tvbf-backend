# NEU-1006 — Entities deleted upstream must not trip the consecutive-failure abort

**Ticket:** [NEU-1006](https://linear.app/neuroticsasquatch/issue/NEU-1006/backend-entities-deleted-upstream-shouldnt-trip-the-consecutive)
**Related:** [NEU-1005](https://linear.app/neuroticsasquatch/issue/NEU-1005) (tombstoning shows), [NEU-967](https://linear.app/neuroticsasquatch/issue/NEU-967), [NEU-961](https://linear.app/neuroticsasquatch/issue/NEU-961)
**Repo:** `tvbf-backend`

## Problem

`INGEST_CONSECUTIVE_FAILURE_THRESHOLD` (default 10) exists to say **"upstream is broken, stop"**. It currently also fires for **"these entities are gone"** — a data condition, not an outage — and aborts an otherwise healthy run.

Measured 2026-08-06 during NEU-967's part-2 cleanup, against a completely healthy TV Maze:

| run | processed | failed | outcome |
| --- | ---: | ---: | --- |
| `d3ce3a7d` | 90 | 24 | `aborted after 10 consecutive failures` |
| `bb2711e6` | 0 | 10 | `aborted after 10 consecutive failures` |

Every failure was a `404 Not Found` on `/shows/{id}`. No 5xx, no timeouts, no rate-limit rejections.

The second abort is the sharp end: it processed **nothing at all**. The work list is `ORDER BY show.id` and deleted shows form a contiguous id band, so the run walked straight into ten of them and died. Failures stamp no watermark, so the same ten head the work list on every retry — **this is a wedge, not a transient condition a retry clears.** It was only escaped by deleting 58 rows by hand.

This is the same bug NEU-961 already fixed one level down, when it moved the credits pass from counting consecutive failed *seasons* to consecutive failed *shows*, and said so:

> The threshold is meant to say "upstream is broken, stop", not "one long-running show is gone".

The same sentence applies with "show" swapped for "entity".

## The insight that makes this small

**The client already retries to exhaustion before anything reaches a caller** (`client.py:120-149`):

- timeouts / network errors — retried, raised only after `retry_max`
- `429` — waits on `Retry-After` and loops, deliberately not counted against the retry budget
- `5xx` — retried, then raised via `raise_for_status`
- everything else, including `404` — raised immediately

So by the time an `httpx.HTTPStatusError` surfaces to a run loop, a **5xx is a persistent upstream failure** and a **404 is a permanent data condition**. The consecutive-failure threshold is a *second* layer of protection on top of retries — which is exactly why conflating the two is wrong, and why classifying at this seam is safe.

Six of the seven affected modules **already catch `httpx.HTTPStatusError` separately**, so the classification seam exists. This is a localised change, not a refactor.

## Decision

**On a 404, leave the consecutive counter untouched — neither increment nor reset.**

Resetting was rejected. A single dead entity landing between 5xx failures would clear the counter and mask an ongoing outage indefinitely:

```
reset:            5xx→1, 404→0, 5xx→1, 404→0, 5xx→1 …  never aborts
leave untouched:  5xx→1, 404→1, 5xx→2, 404→2, 5xx→3 …  aborts correctly
```

Leaving it untouched preserves the threshold's meaning precisely: it counts *persistent upstream failures*, and a gone entity is not one. An outage is still detected even when it happens through a field of dead entities.

A separate persisted `gone` tally was also rejected: it needs a new `ingest_run` column and therefore a migration, and `shows_failed` must keep counting 404s regardless or the verify script's failure-rate band breaks.

## What to build

### 1. One classifier, shared

A single predicate — the whole point is that seven call sites agree on what "gone" means:

```python
def is_gone_upstream(exc: BaseException) -> bool:
    """True when upstream says this entity no longer exists.

    404 only. 5xx, timeouts and network errors have already been retried to
    exhaustion by TVMazeClient._request before they reach a run loop, so one
    that surfaces here is a persistent upstream failure and must still count.
    """
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 404
```

`410 Gone` is deliberately not included — TV Maze does not send it, and adding an unexercised branch is speculative. Add it when upstream does.

### 2. Apply at all seven call sites

| module | counter | note |
| --- | --- | --- |
| `ingest.py` | `consecutive_failures` | already branches on `HTTPStatusError` |
| `update.py` | `consecutive_failures` | already branches |
| `show_refresh.py` | `consecutive_failures` | already branches — this is the one that bit |
| `person_update.py` | `consecutive_failures` | already branches |
| `ratings_backfill.py` | `consecutive_failures` | already branches |
| `akas_backfill.py` | `consecutive_failures` | already branches |
| `episode_credits_backfill.py` | **`consecutive_failed_shows`** | catches bare `Exception`; needs an `HTTPStatusError` branch added |

`episode_credits_backfill` is easy to miss — its counter has a different name, and it is the pass that generated the 245 season-404s in the first place. Its counter is per **show** (NEU-961), so the classification applies at that grain: a show whose every season 404s is gone, and must not increment it.

### 3. What does not change

- **`shows_failed` still counts 404s.** No schema change, and `verify_episode_credits.sh`'s failure-rate band keeps working against the same number.
- **The 5xx / timeout abort path is untouched.** This is the behaviour being deliberately preserved, and it gets its own test.
- **`INGEST_CONSECUTIVE_FAILURE_THRESHOLD` keeps its default of 10.** The threshold was never the problem; what counted toward it was.

### 4. All-404 runs

Log a warning when a run ends having processed nothing but recorded gone-failures. **Do not fail the run.**

Failing would reintroduce the wedge for a legitimately small work list — a daily where the only three due shows all happen to be deleted is a normal Tuesday, not a broken work list. Detecting a genuinely broken work list properly needs the persisted tally that was rejected above; the log line is the honest cheap version, and NEU-1005 makes the situation rare anyway by keeping tombstoned shows out of the feed-derived work list.

## Traps

**Don't classify on the string.** Match `exc.response.status_code`, not the message text.

**Don't widen it to "any 4xx".** A `400` or `401` is a bug in our request or our config, and must still abort — silently absorbing those would be strictly worse than the current behaviour.

**Order the except clauses carefully.** Several sites catch `httpx.HTTPStatusError` and then bare `Exception`; the classification belongs on the former, and the latter must keep counting unconditionally, since an unexpected exception is not evidence that an entity is gone.

## Testing

Unit tests for the predicate; integration tests per behaviour in `tests/integration/tvmaze/`:

1. `is_gone_upstream` — true for a 404 `HTTPStatusError`; false for 500, 400, 401, a timeout, and a bare `ValueError`.
2. A run of ≥ `threshold` consecutive 404s does **not** abort; the run reaches the end of its work list and finalizes `succeeded`.
3. A run of ≥ `threshold` consecutive 5xx failures **still aborts** — the preserved behaviour, tested explicitly.
4. **Interleaved** 5xx and 404 still aborts once 5xx alone reach the threshold — the test that fails under a reset-to-zero implementation.
5. `shows_failed` still counts 404s.
6. `episode_credits_backfill`: a show whose every season 404s does not increment `consecutive_failed_shows`.
7. A run that processes nothing but gone-failures logs the warning and finalizes without failing.

## Acceptance

- A contiguous block of deleted entities can no longer wedge a run
- A genuine upstream outage still aborts, including when interleaved with gone entities
- All seven call sites converted, `episode_credits_backfill` included
- `shows_failed` semantics and the verify script's failure-rate band unchanged
- `task lint`, `task typecheck`, `task test` green

## Out of scope

**Tombstoning the shows themselves.** NEU-1005. Complementary and independent, and neither blocks the other — NEU-1005 reduces how often 404s are encountered; this stops a run dying when they are. Ship this one first if either is urgent: it is much smaller and it unwedges the failure mode on its own.

**The 429 loop.** `_request` retries a 429 indefinitely without consuming retry budget (`client.py:131-139`). Arguably a hang risk, unrelated to this ticket, and no incident has been observed — worth its own look, not a drive-by fix here.

**Watermarking failures.** Making a failed entity stamp something so a retry skips it would also break the wedge, but it changes resumability semantics across every pass and is a much larger design question.
