# NEU-1540 — Split push delivery into two tasks with their own daily caps

**Ticket:** [NEU-1540](https://linear.app/neuroticsasquatch/issue/NEU-1540/separate-different-types-of-notifications-into-separate-tasks-with)
**Project:** tvbf: Maintenance · no parent, no milestone, no relations
**Repo:** `tvbf-backend` only. No frontend change: `sw.js` reads `key`, `title`,
`body`, `url` and `icon`, all of which stay strings. No API change. One
migration (a run-kind CHECK widening, NOT VALID).
**Written:** 2026-10-07, from the `/planit` grilling session.
**Supersedes** the push project spec's Q6 cap, Q7 "fifth scheduled task", §5.2
steps 3 and 5, and §5.5's `PUSH_DAILY_CAP` / `HEALTHCHECK_PUSH_URL`, as marked
`(NEU-1540)` there.

The ticket asks for the three categories of notification — new episodes today,
season premiere set or moved, show ended / cancelled / revived — to be delivered
by **separate tasks with separate maximums per day**, and for each of them to
have **no maximum** by default. Today one job, `python -m tvbf.jobs.push_deliver`,
gathers every kind, applies one per-user cap of five across all of them, and
folds the rest into one summary push. NEU-1539 already made a season dump one
push, so the cap no longer protects against a drop; what it does now is
silence a user who tracks many shows on a busy night, which is the opposite of
what they subscribed for.

Decisions from the session, in the order they were taken:

| # | Question | Decision |
|---|---|---|
| D1 | What is "separate tasks"? | **Two Coolify scheduled tasks**, split along the data source: the schedule-derived **airs-today** set in one, every **catalog event** kind (`premiere_set`, `premiere_moved`, `ended`, `revived`) in the other. Not one job with internal passes; not three tasks. |
| D2 | The maximums | **A cap per task, from env, default unlimited.** `PUSH_AIRS_TODAY_DAILY_CAP` and `PUSH_EVENTS_DAILY_CAP`, `0` = no cap. The cap and summary machinery stays as a dial, off by default. |
| D3 | Overflow when a cap is set | **A per-task summary push**, keyed per task so the two never collide on one day. |
| D4 | Names | **Two new run kinds and entrypoints; the old names are retired.** `push_deliver` stays in the CHECK for historical rows only. |
| D5 | The 90-day purge | **The events task purges both tables.** The airs-today task reads and sends, nothing else. |
| D6 | Schedule | **Different times of day:** airs-today **13:00 UTC**, events **17:00 UTC**. |

---

## 1. Two jobs (`jobs/push_airs_today.py`, `jobs/push_events.py`)

Both are `jobs/scheduled.py` shapes exactly as `jobs/push_deliver.py` is today —
run row, in-flight guard per kind, own deadman, exit code is the result, worker
awaited never spawned — and both keep every rule that module documents:

- **Refuse without VAPID keys**: exit 1, logged, `/fail` pinged, no run row.
- **Exit 1 only when every send failed** (`DeliveryCounts.every_send_failed`,
  404/410 not counting); individual failures are the log's business.
- `shows_processed` / `shows_failed` on the run row carry sent / failed; poll
  through `task ingest:status -- <uuid>`.

| | airs-today task | events task |
|---|---|---|
| entrypoint | `python -m tvbf.jobs.push_airs_today` | `python -m tvbf.jobs.push_events` |
| run kind | `push_airs_today` | `push_events` |
| deadman env | `HEALTHCHECK_PUSH_AIRS_TODAY_URL` | `HEALTHCHECK_PUSH_EVENTS_URL` |
| cap env | `PUSH_AIRS_TODAY_DAILY_CAP` (default `0`) | `PUSH_EVENTS_DAILY_CAP` (default `0`) |
| Taskfile | `task push:airs-today` | `task push:events` |
| Coolify schedule | daily **13:00 UTC** (09:00 US Eastern) | daily **17:00 UTC** (13:00 US Eastern) |
| candidates | `airs_today_candidates` (§5.2 step 1, unchanged) | `event_candidates` (§5.2 step 2, unchanged; `PUSH_EVENT_WINDOW_HOURS` stays) |
| summary key | `summary:airs_today:{user_id}:{date}` | `summary:events:{user_id}:{date}` |
| purge | none | `show_event` + `push_delivery` older than 90 days (step 5, unchanged) |

The in-flight guard is **per kind**, so neither task ever blocks on the other,
and `find_live_run` for one kind never sees the other's row. `today` stays
`now`'s UTC date in both; at both 13:00 and 17:00 UTC that is the US-Eastern
date, the clock `/me/upcoming` reads. Both must still run **after the catalog
delta and the airdate reconcile**, which nothing in the repo can enforce; the
cost of getting it wrong is unchanged (a push a day late).

The 17:00 UTC slot is later than the delta by more hours than before; the 48 h
freshness window (Q6) still covers last night's events comfortably, and the
still-current check runs at send time as it does today.

## 2. Delivery (`push/delivery.py`)

`run_push_delivery` is generalised into one pass parameterised by **which
candidates, which cap, which summary key, and whether to purge**, rather than
two near-copies. A small frozen `DeliveryTask` (or equivalent — the shape is
the implementer's) carries:

- `kind` (the run kind, `push_airs_today` / `push_events`) and a `name` for logs
- a candidate gatherer: `airs_today_candidates(session, today=…)` or
  `event_candidates(session, now=…, window_hours=…)`
- the cap value, read from the matching `Settings` field
- the summary task label (`airs_today` / `events`) the key and title are built from
- `purges: bool`

`run_push_delivery_job` finalizes exactly as today. `DeliveryCounts` is
unchanged; `purged` is `0` for the airs-today task.

**Steps 4 and 6 of §5.2 are unchanged**: the two-transaction claim-then-send,
the `(notification_key, subscription_id)` idempotency rule, the stale-`pending`
re-claim, 404/410 retirement, the five-failure limit, the exit-code rule.

## 3. Cap and summary (`push/candidates.py`)

`apply_cap` gains the per-task shape:

- `cap` is an `int`, and **`0` means no cap**: every candidate is kept and no
  summary is ever built. Negative values are rejected at settings load
  (`ge=0`), not here.
- The summary `key` takes the task label: `summary:{task}:{user_id}:{date}`.
  Two tasks on one day are two keys, so the second's summary is never a
  `skipped` claim against the first's `sent` row.
- The summary `Candidate` gains the task label (a new field, `task` or the
  like) so `payloads.build_payload` can title it.
- Ordering within a task is what it is today, minus the other half: airs-today
  by article-stripped show name, then season and episode; events by
  `observed_at` then `event_id`. The cross-kind "airs-today first" rule no
  longer applies inside one task and is deleted, not kept dormant.
- The `summary` candidate's `count` and `show_names` semantics are unchanged.

Nothing about the airs-today fold (NEU-1539), the Q16 clauses, the freshness
window or `is_still_current` changes. **Events are not grouped per show**: a
show whose premiere is set and then moved inside 48 h is two events, and the
still-current check already drops the superseded one; Q8's one push per show
*per kind* stands.

## 4. Payload (`push/payloads.py`)

Only the summary title changes, by task:

| task | title | body | url |
|---|---|---|---|
| airs-today | `{N} more shows air today` (`1 more show airs today`) | the remainder's distinct show names, as today | `/upcoming` |
| events | `{N} more updates today` (`1 more update today`), unchanged | as today | `/upcoming` |

Every other kind's payload is byte-for-byte what it is now.

## 5. Settings (`config.py`) and the migration

- Remove `healthcheck_push_url` / `HEALTHCHECK_PUSH_URL` and `push_daily_cap` /
  `PUSH_DAILY_CAP`.
- Add `healthcheck_push_airs_today_url` / `HEALTHCHECK_PUSH_AIRS_TODAY_URL`,
  `healthcheck_push_events_url` / `HEALTHCHECK_PUSH_EVENTS_URL` (both optional,
  default unset), `push_airs_today_daily_cap` / `PUSH_AIRS_TODAY_DAILY_CAP` and
  `push_events_daily_cap` / `PUSH_EVENTS_DAILY_CAP` (both `int`, `ge=0`,
  default `0`).
- `push_event_window_hours` / `PUSH_EVENT_WINDOW_HOURS` stays.
- One Alembic migration, head `a964646910d2`, widening `ck_ingest_run_kind` to
  add `'push_airs_today'` and `'push_events'`, **NOT VALID**, in the shape of
  `a4e9c7d2b813`; `'push_deliver'` **stays in the list** so the historical run
  rows remain valid, and `catalog/models.py`'s mirror of the CHECK gains the
  two kinds the same way. Downgrade drops the two new kinds.

## 6. Retired

Deleted, not deprecated: `src/tvbf/jobs/push_deliver.py`, `task push:deliver`,
`HEALTHCHECK_PUSH_URL`, `PUSH_DAILY_CAP`, and the `summary:{user_id}:{date}`
key shape. Nothing in the frontend or any endpoint references any of them.
Rows of kind `push_deliver` in `catalog.ingest_run` and old `summary:` keys in
`app.push_delivery` stay as history and purge on their own schedule.

## 7. Documentation

- `docs/specs/tvbf-push-notifications-project-spec.md`: Q6 (cap per task,
  default unlimited), Q7 (two tasks), §5.2 steps 3 and 5, §5.3 summary title,
  §5.5 env table. Each edit marked `(NEU-1540)` and pointing here.
- `CONTEXT.md` **Notification**: the cap is per task and optional (done in the
  session).
- `.claude/CLAUDE.md`: the `task push:deliver` paragraph becomes two short
  paragraphs, `task push:airs-today` and `task push:events`, keeping the
  idempotency and exit-code rules once and pointing at this spec.
- `README.md`: the scheduled-task paragraph and the env table rows.
- `Taskfile.yml`: the two tasks replace `push:deliver`.

## 8. Deploy runbook (PR description)

1. Create two healthchecks.io daily checks; set `HEALTHCHECK_PUSH_AIRS_TODAY_URL`
   and `HEALTHCHECK_PUSH_EVENTS_URL` in Coolify's env. Remove
   `HEALTHCHECK_PUSH_URL` and `PUSH_DAILY_CAP` and retire the old check.
2. Run the migration (the kind CHECK) with the deploy, before either task fires.
3. Replace the `python -m tvbf.jobs.push_deliver` scheduled task with
   `python -m tvbf.jobs.push_airs_today` at **13:00 UTC** and
   `python -m tvbf.jobs.push_events` at **17:00 UTC**.

**Deploy-day note.** Notification keys for every real kind are unchanged, so a
day's airs-today or event push already logged `sent` by the old job is a
`skipped` claim under the new one. Only the summary key changed, and no summary
is sent at the default cap, so there is no double send on deploy day.

## 9. Tests

- `tests/integration/jobs/test_push_deliver_cli.py` is split into
  `test_push_airs_today_cli.py` and `test_push_events_cli.py`, each keeping the
  cases that apply to it (claim, retry, retirement, exit rule, refusal, in-flight,
  own check pinged); the purge test lives with events only, and one new
  airs-today test asserts the purge did **not** run. The season-dump test keeps
  its NEU-1539 assertions.
- `tests/unit/push/test_candidates.py`: cap `0` keeps everything and builds no
  summary; a positive cap still summarises; the summary key carries the task
  label; the ordering tests drop the cross-kind case and keep the per-kind ones.
- `tests/unit/push/test_payloads.py`: the two summary titles.
- `tests/unit/test_config.py`: the four new fields, their defaults, and that a
  negative cap is rejected.
- `tests/unit/jobs/` or wherever the run-kind CHECK is asserted: both new kinds
  accepted, `push_deliver` still accepted.

## 10. What this does not do

- Does not change any real kind's key, body, url or icon.
- Does not group events per show or merge kinds.
- Does not touch the freshness window, the still-current check, Q16, or the
  NEU-1539 fold.
- Does not touch `POST /me/push/test`, `GET /admin/push/stats`, the preferences
  or the mute.
- Does not touch the frontend.

## 11. Acceptance criteria

1. `python -m tvbf.jobs.push_airs_today` sends every airs-today candidate for
   every subscribed user with no cap by default, logs each under its unchanged
   `airs_today:{show_id}:{date}` key, pings only `HEALTHCHECK_PUSH_AIRS_TODAY_URL`,
   runs no purge, and finalizes a `push_airs_today` run row.
2. `python -m tvbf.jobs.push_events` sends every fresh, still-current catalog
   event with no cap by default, purges 90-day-old `show_event` and
   `push_delivery` rows, pings only `HEALTHCHECK_PUSH_EVENTS_URL`, and finalizes a
   `push_events` run row.
3. With `PUSH_AIRS_TODAY_DAILY_CAP=2` and three shows airing, a user gets two
   airs-today pushes and one `summary:airs_today:{user}:{date}` push titled
   `1 more show airs today`; the same day with `PUSH_EVENTS_DAILY_CAP=1` and two
   events, one event push and one `summary:events:{user}:{date}` push titled
   `1 more update today`. Both summaries land; neither is skipped.
4. One task already in flight does not stop the other from running.
5. `python -m tvbf.jobs.push_deliver`, `HEALTHCHECK_PUSH_URL` and
   `PUSH_DAILY_CAP` no longer exist; the migration leaves old `push_deliver` run
   rows valid.
6. `task test`, `task lint` and `task typecheck` pass.
