# NEU-1149 — Scope the nightly airdate work list to change

**Ticket:** [NEU-1149](https://linear.app/neuroticsasquatch/issue/NEU-1149/backend-scope-the-nightly-airdate-work-list-to-change-not-to-catalog)
**Repo:** `tvbf-backend`
**Project:** TVBF: TMDB Migration · Milestone 5, Migration & cutover
**Related:** [NEU-1145](NEU-1145-airdates-one-day-early.md) (merged, run in production 2026-08-14 — this spec amends its §4.4), [NEU-1148](NEU-1148-cache-the-tv-maze-show-id.md) (merged — this spec adds a column to its §3 table)
**Status:** approved for implementation

This spec lives in-repo rather than in the umbrella `docs/` for the reason
[NEU-1148](NEU-1148-cache-the-tv-maze-show-id.md) gives and one more: it
**corrects a measured claim in NEU-1145 §4.4** and **adds a column to NEU-1148
§3's table**, so both of those documents cite it back. A correction that cannot
be linked from the thing it corrects is not a correction.

---

## 1. What this is for

The nightly airdate reconciliation re-derives every offset from scratch every
night. That was deliberate — §4.4 says "re-checking everything is what makes the
job self-healing" — but it makes the run's cost proportional to **catalog size
plus user count**, where the thing that actually needs re-checking is
proportional to **change**.

NEU-1148 halved the cost of each unit of that work. This ticket reduces how many
units there are, and is the one that touches scaling: halving a growing number
still grows.

### 1.1 §4.4's work-list claim is wrong, and the shape it hides

§4.4 states the tracked and future-episode halves "nearly coincide". Measured
against production 2026-08-14, they are nearly **disjoint**:

| set | shows |
| -- | -- |
| tracked by a user | 560 |
| holding a future-dated episode | 1,787 |
| **in both** | **27** |
| union (in scope) | 2,320 |
| minus those with no external id | **1,772 checkable** |

Correcting that sentence is AC 7. What it hides is that the run is two
populations doing two different kinds of waste:

- **1,760 shows are airing and untracked.** Reconciled nightly for nobody in
  particular — though not for nothing; see §3.
- **533 shows are tracked and finished.** Their airdates will never change
  again, and we re-derive them every night regardless. **This is the population
  that grows with the user base**, and it is exactly where nightly re-checking
  buys nothing.

---

## 2. The trigger already exists

Both the daily delta and the full pass reach `upsert` through
`tmdb/ingest.py:mirror_series`, which calls `mark_series_synced`. So
`catalog.show.tmdb_synced_at` advances **precisely when TMDB reported a change
and we re-mirrored the show**. No new column and no new signal is needed to
answer "did TMDB touch this show since we last reconciled it?"

What is needed is the other half of the comparison: a per-show
`last_reconciled_at`.

---

## 3. What the predicate does — and what it does not

**Scope is unchanged.** A show is ours to correct if a user tracks it **or** it
holds a future-dated episode. The four clauses below are **ANDed onto that
scope**, never substituted for it — `last_reconciled_at IS NULL` alone matches
all ~229k mirrored shows, which would turn a 23-minute run into the ~32-hour one
§4.4 explicitly refused.

A show in scope is **due** tonight when any of:

```sql
last_reconciled_at IS NULL                    -- never done: new show, or newly tracked
OR show.tmdb_synced_at > last_reconciled_at   -- TMDB changed it since we last looked
OR <the scope's own future-episode predicate> -- still airing, so dates can still move
OR <the sweep is due for this show>           -- §5, the oracle-side drift backstop
```

**"Unaired" reuses the scope's existing predicate exactly** —
`coalesce(tmdb_air_date, air_date) > current_date` — rather than becoming a
second concept that also counts NULL-dated episodes. A NULL date cannot be
compared, so it cannot produce a verdict; widening would admit an unbounded
population of shows carrying undated episode rows in exchange for nothing.

### 3.1 What this actually buys, stated honestly

The third clause readmits **every airing show every night**, including the 1,760
nobody tracks. So the steady-state nightly list does **not** become proportional
to change. It becomes proportional to *what is currently airing*, plus what
changed:

| | shows |
| -- | -- |
| today, every night | ~1,772 checkable |
| after, a normal night | airing + changed + never-done + one seventh of the scope, **less the tracked-finished population** |

**Measured on 2026-08-14 against production** (before the pass had run there, so
the "after" is the predicate evaluated over the same rows): 1,772 checkable in
scope, 1,242 of them airing and 530 tracked-and-finished. Per sweep bucket the
nightly list is 1,321 / 1,320 / 1,306 / 1,315 / 1,320 / 1,336 / 1,306 — mean
~1,318 against 1,772 every night, with the 530 that grow with the user base
costing ~76 a night rather than 530, and a spread of 30 shows confirming no
night carries the sweep. The estimates in this table were written before that
measurement; the shape they predicted holds and the magnitudes were off, because
1,787 shows hold a future episode but only 1,242 of those are checkable.

**The ticket's AC 1 as written overstates this and is rewritten in §8.** The
defensible claim — and the one worth making — is that the run's cost is
**decoupled from user count and from catalog history**. The airing population is
bounded by how much television exists at once; it does not grow because the
catalog accumulates back-catalogue, and it does not grow because you gain users.
The tracked-finished population does grow with users, and it is the one this
change removes from the nightly list.

### 3.2 Why the airing clause is kept

Dropping it would make the list genuinely change-bounded — a few dozen shows a
night. It is kept because **an airing season's evidence changes on the oracle's
side by nature, and no watermark of ours can see that**. TV Maze adds episodes
as they are announced, which is precisely what turns a season refused for
`too_few` (one comparable episode) into one that can finally reach a verdict.
TMDB may not change at all in that window, having entered the full season
already, so nothing in clauses 1, 2 or 4 would fire until the sweep. Nightly
re-checking of airing shows is the cheapest way to keep a currently-airing
season correct within a day, which is the case a user actually notices.

### 3.3 Rejected alternatives

Carried from the ticket, with one addition.

- **A plain "don't re-check within N days" interval.** Decouples work from need
  in *both* directions: a finished show re-checked every 30 days is still waste,
  and a show whose schedule shifts waits up to 30 days. Useful only as the
  sweep backstop, which is what §5 makes it.
- **Reconciling only currently-airing or future seasons.** Right instinct,
  wrong grain: `/shows/{id}/episodes` returns the whole series in one request
  whatever seasons we care about, so filtering seasons saves **no requests at
  all**. The useful form of the idea is at show grain, and it is clause 3.
- **Dropping the untracked half of the work list.** Cuts 1,760 shows
  immediately and is wrong: that half exists so a show is already correct the
  moment somebody tracks it, rather than a night later.
- **Restricting clause 3 to airing shows a user tracks** (27 shows). Rejected
  for the same reason as the point above — it keeps the untracked airing shows
  in *scope* while denying them the only clause that ever fires for them, which
  is the same thing as dropping them with extra steps.

---

## 4. The watermark

`last_reconciled_at TIMESTAMPTZ NULL` on **`catalog.airdate_show_state`** — the
table NEU-1148 §3 created and deliberately named for *what the airdate pass
knows about a show*, anticipating exactly this column. One table, not two.

Nullable, because NEU-1148's rows predate it and because NULL is the "never
done" state clause 1 reads.

**What stamps it: a show's turn completing without raising.** That covers both
a real comparison and "TV Maze has no counterpart" — the latter is a genuine
conclusion, and leaving it unstamped would keep the ~500-show negative
population in every night's work list forever, which is the failure mode
NEU-1148's negative cache exists to prevent one grain down. It now costs no TV
Maze request (the negative link is cached), but it still costs a session, two
queries and a line in the number AC 1 measures.

**What does not stamp it: a show that raised.** It is left at its previous value
(or NULL), so tomorrow retries it — the same self-healing shape as the per-show
failure contract that counts a failure without aborting the run.

**Where the write goes: one call site**, in `run_airdate_reconcile`'s success
branch, in the same session and transaction as the show's own work, immediately
before its commit. Not inside `_reconcile_show`, which returns early on
"no counterpart" and would need the stamp twice; and not in `_reconcile_show`'s
caller *after* the commit, which would let a crash between the two silently
re-do the show. An upsert rather than an update, so it does not depend on
NEU-1148's link row having been written first.

**Shows with no external id are untouched.** All 548 of them are filtered out in
Python after the query and named in a warning, per the no-silent-caps rule, and
that does not change. They never reach a stamp, never leave the SQL work list,
and cost nothing but the row. The number AC 1 measures is therefore the
**checkable** set, which is what §1.1's table already reports.

---

## 5. The sweep

§4.4's self-healing claim is real, and this ticket trades some of it away, so it
has to buy it back explicitly. Clauses 1–3 catch every change **on TMDB's
side**. None of them can see a change on the **oracle's** side — TV Maze
correcting a date on a finished show TMDB never touches. Without a sweep, an
offset that should be retracted never would be.

**`SWEEP_DAYS = 7`.** A module constant beside `MAX_OFFSET_DAYS` and
`MIN_EPISODES`, which are the precedent NEU-1148 §4 set: rules about what the
pass believes, changed by a code change with a test rather than by an operator.

**Amortised, not periodic-in-bulk.** A show's bucket is `show_id % SWEEP_DAYS`
and tonight's bucket is `<days since epoch> % SWEEP_DAYS`, so **one seventh of
the scope is swept every night** and every show is still swept weekly.

The alternative — a plain `last_reconciled_at < now() - 7 days` — was rejected
on a specific failure: the first run after deploy stamps every show on one
night, and because a sweep restamps everything it touches, that synchronisation
is *permanent*. Six quiet nights and one that reconciles the entire scope,
forever — and the entire scope includes the tracked-finished population that
grows with users. A synchronised sweep reimports the scaling problem one night
in seven. Bucketing on `show_id` makes a spike unreachable by construction,
because the bucket does not depend on when the show was last stamped.

**Days-since-epoch, not day-of-week**, so `SWEEP_DAYS` stays a real constant. A
day-of-week comparison silently pins the interval to 7.

**A staleness floor at `2 * SWEEP_DAYS`.** The bucket rule alone means a missed
night — container down, or a run that aborted on the consecutive-failure
threshold before reaching those shows — costs that bucket a full extra interval.
So the sweep clause is:

```
bucket matches tonight OR last_reconciled_at < now() - 2 * SWEEP_DAYS
```

The floor fires only when shows are genuinely overdue, which is exactly when a
spike is wanted. At 1× it would fire for the whole scope one week after the cold
start, reintroducing the synchronisation the bucketing removed.

**The cold start needs no backfill.** The first run after deploy has every
`last_reconciled_at` NULL, so it reconciles the full scope — one night at
today's cost, the same shape as NEU-1148's cold start. From the next night
buckets spread the sweep immediately, and the 2× floor never fires.

---

## 6. Ordering against the catalog delta

NEU-1145 §4.4 deliberately kept this pass off the delta's schedule and off its
healthcheck, and that stays right: one check fed by two tasks lets either keep
it alive while the other quietly stops.

But clause 2 makes the *order* matter for latency, where before it mattered for
nothing. If the airdate pass runs before the delta, it reads yesterday's
`tmdb_synced_at` and picks a change up a night late.

**Decision: order the airdate task after the catalog delta in Coolify.** A delta
is minutes, so a comfortable gap is enough. The two cannot contend — separate
`catalog.rate_budget` rows against separate upstreams — so this is purely about
latency.

**This is a Coolify cron edit, and nothing in this repo can enforce it.** The
runbook records the intent; if the times are never changed, the behaviour
degrades to the accepted-but-not-chosen alternative, which is a one-night lag on
delta-driven changes for **finished** shows only. Airing shows are reconciled
nightly by clause 3 regardless of ordering, and the sweep bounds everything else
at a week, so the failure is bounded and quiet rather than dangerous.

---

## 7. What the run reports

The pass logs "N show(s) in scope" today, which after this change cannot answer
the question the ticket is about: a 1,790-show night looks identical whether it
is 1,760 airing shows plus 30 changes, or a sweep clause misfiring.

**The work-list query returns one boolean per clause**, and the run logs the
breakdown beside the existing counts:

```
airdate reconciliation: 1,790 show(s) due — 1,760 airing, 12 changed,
3 never reconciled, 331 swept
```

Four `CASE`s in the `SELECT` and no extra round trip. This is what makes AC 1
readable off a production run rather than off hand-written SQL, and it is what
makes "which population is growing" a thing you can watch rather than
re-derive. The flags travel on `ShowToCheck` as a small frozen `DueReasons`
record; the counters are summed in `run_airdate_reconcile`.

**Deliberately not persisted to `catalog.ingest_run`.** That table is shared by
every run kind, and a column or JSON blob for one kind's clause attribution
earns its place only once somebody wants the trend across nights. The log is
where this lives until then.

---

## 8. Acceptance criteria

1. **The steady-state nightly work list is decoupled from user count and
   catalog history** — bounded by what is currently airing, what TMDB changed,
   what has never been done, and one seventh of the scope. Measured before and
   after and recorded on the ticket. Before: 2,320 in scope / 1,772 checkable,
   every night. Expected after, on a normal night: the 533 tracked-finished
   shows leave, and ~330 return as the sweep slice. **This replaces the
   ticket's AC 1**, which claimed proportionality to change and is not what the
   agreed predicate delivers — see §3.1.
2. A newly tracked show is still corrected on the next run (clause 1).
3. A show the TMDB delta touched is re-reconciled on the next run (clause 2).
4. A currently-airing show is still re-reconciled every night (clause 3).
5. A finished, untouched show is reconciled once and then skipped until its
   sweep turn (clauses 1 and 4).
6. The sweep runs on a documented cadence, is amortised so no night spikes, and
   **the module docstring says the sweep is what preserves §4.4's self-healing
   property**.
7. §4.4's "the two sets nearly coincide" is corrected in
   `docs/specs/NEU-1145-airdates-one-day-early.md` — 27 of 2,320 overlap — along
   with its "no watermark, the full list runs every night" paragraph, which this
   ticket makes wrong.
8. Ordering against the catalog delta is decided and written down (§6), in the
   spec and in `docs/migration/README.md`.
9. A show that raised during its turn is retried on the next run rather than
   skipped to its sweep turn.
10. No test makes a live TV Maze call.

---

## 9. Testing notes

- **The sweep needs an injectable "today"**, or its test asserts something that
  changes with the calendar. `shows_to_check` takes an optional day index
  defaulting to the database's own clock — the same shape NEU-1148's
  `relookup_missing_after` override takes, and for the same reason.
- **One test per clause**, each seeding a show that is in scope and due for
  exactly one reason, plus one asserting a finished untouched show is *not*
  due — the negative case is the whole ticket.
- **A two-run test**: after a first run stamps everything, a second run's work
  list is the airing set plus tonight's sweep bucket, and specifically does not
  contain the tracked-finished show.
- **A test that a raising show is due again next run**, which is AC 9 and the
  one place the stamp's placement is observable.
- Fixtures seeding `catalog` need `await session.flush()` between parent and
  child inserts — there are no `relationship()` declarations.
- Tests asserting a row after an upsert need
  `execution_options={"populate_existing": True}`.
- Autouse fixtures must not request `monkeypatch`.
- New `result.rowcount` sites need `# type: ignore[attr-defined]`, and
  CLAUDE.md's list of such files needs updating
  (`grep -rln 'rowcount.*type: ignore' src/` is authoritative).
- Adding a column to `catalog` touches none of the five schema-enumeration
  sites, but CLAUDE.md's DB-topology entry for `airdate_show_state` names its
  columns and does need the new one.
- Run `task format` before committing.

---

## 10. Documentation to update

- `docs/specs/NEU-1145-airdates-one-day-early.md` §4.4 — the coincidence claim
  and the no-watermark paragraph (AC 7).
- `docs/specs/NEU-1148-cache-the-tv-maze-show-id.md` §3 — a line noting the
  column landed, since that section predicted it.
- `src/tvbf/airdates/reconcile.py` module docstring — the "No watermark"
  paragraph is now false, and the sweep's role in preserving self-healing goes
  here (AC 6).
- `docs/migration/README.md` — the delta ordering decision, and the fact that
  the NEU-1148 escape hatch `DELETE FROM catalog.airdate_show_state` now also
  clears every watermark, so the next run re-derives the full scope. Correct
  behaviour for a "start over" hatch, but not something to discover.
- `CLAUDE.md` — the `reconcile:airdates` bullet and the `airdate_show_state`
  entry in the DB-topology section.

---

## 11. Out of scope

- **Widening the work list to the full catalog.** Still refused, per NEU-1145
  §9, and this change does not alter the etiquette argument behind it.
- **Persisting the clause breakdown to `catalog.ingest_run`** (§7).
- **Making the airing clause cheaper.** The `EXISTS` over `catalog.episode` runs
  per work-list query and is served by
  `ix_episode_show_id_season_number`; the run is rate-limiter-bound, not
  DB-bound, so this is not where time goes.
- **Reconciling on demand when a user tracks a show.** Clause 1 covers it by the
  next run, which is the latency NEU-1145 already accepted.
