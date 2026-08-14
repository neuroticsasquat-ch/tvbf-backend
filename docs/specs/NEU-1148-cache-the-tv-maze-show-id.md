# NEU-1148 — Cache the TV Maze show id so the airdate pass stops re-resolving it

**Ticket:** [NEU-1148](https://linear.app/neuroticsasquatch/issue/NEU-1148/backend-cache-the-tv-maze-show-id-so-the-airdate-pass-stops-re)
**Repo:** `tvbf-backend`
**Project:** TVBF: TMDB Migration · Milestone 5, Migration & cutover
**Related:** [NEU-1145](NEU-1145-airdates-one-day-early.md) (merged, run in production 2026-08-14), [NEU-1146](NEU-1146-retire-the-tv-maze-orphan-rows.md), NEU-1149
**Status:** approved for implementation

This spec lives in-repo rather than in the umbrella `docs/` for the same reason
[NEU-1145](NEU-1145-airdates-one-day-early.md) does, and one more: NEU-1145 §6 is
amended by §7 below and must cite *this* document for why the extraction widened,
which it can only do if this file is version-controlled and permalinkable. NEU-1149
must likewise cite §3's table shape, since it adds a column to it.

---

## 1. What this is for

The nightly airdate reconciliation spends **two TV Maze requests per show**: a
`/lookup/shows` by external id, then one `/shows/{id}/episodes`. The first is pure
re-derivation — the same external ids produce the same answer every night, and we
throw the answer away every night.

The pass is entirely rate-limiter-bound, so halving the requests roughly halves the
wall clock. Measured on the first production run, 2026-08-14:

| | |
| -- | -- |
| shows in scope | 2,320 |
| carrying no `imdb_id` or `tvdb_id` (skipped before any request) | 548 |
| **checkable** | **1,772** |
| pace | **~1.13 s/show** — exactly 2 requests at TV Maze's 18-per-10s |
| wall clock | **~23 minutes** |

The pace figure is the one that decides the design: at 1.13 s/show against a budget
of 18 requests per 10 seconds, the run is *paced*, not compute- or DB-bound. Nothing
but the request count moves it. Expected after this change: **~12 minutes**.

**This ticket does not fix scaling.** The work list grows with catalog size and user
count, and halving a growing number still grows. NEU-1149 is the change that makes
the run bounded by *change*; this one makes each unit of that work cost half. Do
this one first, so NEU-1149's `last_reconciled_at` lands in the table §3 creates
rather than needing a second migration.

---

## 2. Why the id does not go on `catalog.show`

Two reasons, and the first is the binding one.

**ADR-0012 says the catalog is sole-sourced from TMDB.** `catalog.show.tvdb_id` and
`catalog.show.imdb_id` are TMDB's own `external_ids`, mirrored like any other field.
A TV Maze id is not from TMDB, and putting it on the spine erodes exactly the
invariant NEU-1146 deleted 782,161 episode, 18,341 season and 2 show rows to
establish. The same reasoning already put `catalog.air_date_offset` in a table of its
own rather than a column on `catalog.season`, and for the same structural payoff:
"the TMDB ingest never writes one, it only reads one" becomes a property visible in
the schema instead of a rule someone has to remember.

**It gives the negative result a home.** A column on `catalog.show` cannot
distinguish *never asked* from *asked, no counterpart* — both read NULL. §4 is
entirely about why that distinction is the whole point.

---

## 3. The table

```sql
CREATE TABLE catalog.airdate_show_state (
    show_id     BIGINT PRIMARY KEY REFERENCES catalog.show(id) ON DELETE CASCADE,
    tvmaze_id   INTEGER NULL,
    resolved_at TIMESTAMPTZ NOT NULL
);
```

**Named for what the airdate pass knows about a show**, not for the one column it
holds today. NEU-1149 adds `last_reconciled_at` here — a watermark about *our* work,
not about the oracle's identity for the show — and that column belongs under this
name without strain where it would sit oddly under `airdate_oracle_link`. One table
for what the pass knows, not two.

**`show_id` is the primary key directly.** No surrogate `id` plus a unique
constraint: one row per show is the table's entire meaning, and unlike
`air_date_offset` there is no second key part that would make a surrogate earn its
place. `ON DELETE CASCADE` matches `air_date_offset`.

**`resolved_at` means "when we last asked TV Maze about this show"** — one meaning
covering both row shapes. For a row carrying an id it is when that id was
established; for a row carrying NULL it is when we last looked and found nothing,
which is what §4's expiry clause compares against. `NOT NULL`, so *never asked* is
the absence of a row and nothing else.

**No `UNIQUE (tvmaze_id)` and no index on it.** Two of our shows legitimately
resolving to one TV Maze show is not hypothetical — it is the same shape NEU-1146
spent four match tiers on: TMDB models the *Will & Grace* revival as a separate
series and *Cunk on Earth* as a season of an anthology, where another catalogue
keeps each whole. A unique constraint would fire on correct data, aborting one show
mid-pass and, after ten in a row, the entire run. The bug it would catch — a wrong
external id pointing two shows at one oracle — is already contained by the trust
rule: both shows would be judged against the same episode list, and a mismatched
pairing produces inconsistent per-episode deltas, which is a refusal in the log
rather than a wrong offset. No index either; every access is by `show_id`.

**The table is a pure derived cache.** Nothing in it cannot be rebuilt by asking TV
Maze again, which is what makes §8's escape hatch a bare `DELETE`.

---

## 4. The caching policy

Three states, and the middle one is the reason the table exists.

| state | meaning | what the pass does |
| -- | -- | -- |
| no row | never asked | look up, write the result |
| row, `tvmaze_id` set | resolved | reuse it; spend no lookup |
| row, `tvmaze_id` NULL | asked, no counterpart | reuse the negative until it expires |

**`tvmaze_id IS NULL` must mean "looked and found nothing."** Without the negative
cache the ~500 shows TV Maze has never heard of get re-looked-up every night
forever, which is most of the saving gone. `resolved_at` is what distinguishes
*never asked* from *asked, no counterpart* — the same distinction
`catalog.show.credits_synced_at` exists to make one grain up, where "the show has no
`show_cast` row" could not tell *upstream has none* from *nobody asked* and so would
never converge.

**A NULL is re-looked-up after `RELOOKUP_MISSING_AFTER = timedelta(days=30)`.** A
negative is not permanent: a show TV Maze adds later should eventually be found.
Thirty days is ample, because this population is specifically *shows TV Maze has
never heard of* — the ~548 shows with no external id at all never reach a lookup and
never get a row — so nobody can currently correct them by any means, and a month's
latency on one appearing costs nothing.

**A module constant, not a setting.** It sits beside `MAX_OFFSET_DAYS = 1` and
`MIN_EPISODES = 2`, which are the precedent: rules about what the pass *believes*,
changed by a code change with a test, not by an operator. A setting would need an
entry in `config.py`, `.env.example`, the Coolify scheduled task's environment and
CLAUDE.md, and there is no scenario where prod and dev should disagree about it —
unlike `INGEST_STALE_RUN_MINUTES`, which is a setting precisely because run liveness
depends on deploy cadence. Tests take an explicit override argument rather than
waiting thirty days.

**A resolved id is never re-looked-up on a timer.** AC 1 is that a show resolved
once is not looked up again, and §5's invalidation is what handles the case where
that id stops working.

### 4.1 External-id changes do not invalidate a link

The input to the lookup can change under a cached answer, because
`catalog.show.imdb_id` / `tvdb_id` are TMDB's `external_ids` and are refreshed
whenever a delta re-mirrors the show. Three cases, and they are not equally
worrying:

1. **No external id → gains one.** Already handled with no machinery at all:
   `run_airdate_reconcile` filters those shows out of `checkable` *before* any
   lookup, so they never get a row. The night TMDB gives one an `imdb_id` it is
   checkable and resolved fresh. This is the common case.
2. **Cached NULL, ids later change.** The stored answer is to a question no longer
   being asked. Bounded by the same 30 days §4 already accepts for the identical
   "TV Maze might have it now" reason.
3. **Cached id, ids later change.** We keep using an id derived from a since-
   corrected external id. Undetectable without a re-lookup we have decided not to
   make; lands on the trust rule, which refuses inconsistent evidence rather than
   writing a wrong offset.

**Considered and rejected: storing the external ids as they stood at resolution**,
re-resolving when either differs. It would make "the answer is stale because the
question changed" structural rather than time-bounded, at the cost of a
denormalised copy of two TMDB columns in a table whose entire justification (§2) is
keeping non-TMDB data off the spine — here it would be TMDB data off the spine,
which is the mirror-image oddity. Rejected because case 1 is free and cases 2 and 3
require TMDB to *correct* a stable identifier, which is rare.

**Considered and rejected: comparing `resolved_at` against `show.tmdb_synced_at`.**
No new column, and superficially attractive. It is wrong here and it must not be
adopted by analogy: a delta re-mirrors currently-airing shows often, and most of the
work list *is* currently-airing shows, so this would invalidate links constantly and
gut the cache. That comparison is the right trigger for NEU-1149's *reconciliation*
watermark — "did TMDB touch this show since we last reconciled it?" — and the wrong
one for link identity, which is about who the show *is*, not about what changed on
it.

---

## 5. A stale link, and why it needs handling

This is the one failure mode the change introduces, and left alone it is invisible.

Today a show whose TV Maze entry disappears or is merged resolves to `None` every
night, lands in `shows_not_found`, and is logged by name. With a cached id we stop
asking, and `/shows/{id}/episodes` 404s instead — which currently returns `[]`. Every
season then judges `no_overlap`, `decided` is empty, and `replace_season_offsets`
leaves every season alone by design (a refusal is the absence of a verdict, not a
verdict of zero). The show silently stops being reconciled forever, counted only in
`seasons_uncomparable`, which is already a large ordinary number — season 0 alone
puts most shows there.

**The fix: `get_show_episodes` distinguishes 404 from empty, and the pass
invalidates and retries once.**

- The client returns `None` for a 404 and `[]` for a show TV Maze genuinely carries
  with no episodes. After this, `[]` means only the latter, and the docstring must
  say so — the same `None`-vs-`[]` distinction `tmdb/upsert.py` already draws
  between "the caller did not append this namespace" and "upstream has none".
- On `None`, clear the cached id, re-run `lookup_show` once, and if it resolves,
  fetch episodes again. **Three requests for that show that night, one thereafter.**
  AC 1's claim is about steady state, and a stale link is by definition not steady
  state.
- Count it. `links_invalidated` on `ReconcileResult`, in the closing log line, so a
  run that is quietly re-resolving many shows is visible rather than merely fast.

Not handling this at all was considered and rejected: it converts a logged,
countable, per-show event into a permanent invisible one, which is the opposite of
the no-silent-caps rule the pass already follows for shows with no external id.

**Accepted and not designed for:** if TV Maze *merges* two shows and the old id
keeps serving episodes for the wrong series, no re-lookup catches it, because we
never make one. The trust rule's unanimity clause would refuse rather than write a
wrong offset, so the failure surfaces as a refusal in the log. A periodic positive
re-check to catch it would cost the saving this ticket exists to make.

---

## 6. The seam

A new module, `src/tvbf/airdates/show_state.py`, owns the table's rules and exposes
verbs rather than rows — the shape `catalog/offsets.py` already has.

**Under `airdates/`, not `catalog/`.** Unlike `offsets.py`, which the TMDB ingest
reads on every write, nothing outside the airdate pass touches this table, and
nothing ever will. `catalog/` is where the catalog's own rules live; this is the
airdate pass's bookkeeping about its oracle.

**One entry point, deep rather than shallow:**

```python
async def oracle_episodes(
    session: AsyncSession, client: TVMazeOracleClient, show: ShowToCheck
) -> list[TVMazeEpisode] | None
```

`None` means "no TV Maze counterpart". `[]` means "a counterpart with no episodes".
The module owns the whole path — read the link, spend a lookup only if there is not
a usable one, fetch episodes, and on §5's 404 clear the link, re-resolve once and
re-fetch — and reports what it spent so the counters come from the one place that
knows.

`_reconcile_show` collapses from a lookup, a None-check and an episode fetch to a
single call, and never learns a cache exists. The alternative — exposing
`load_link` / `record_link` / `clear_link` and orchestrating in `_reconcile_show` —
was rejected: it pushes five branches of caching policy into the function whose
subject is the trust rule, and puts the retry-once loop somewhere a test can only
reach through a full pass.

The module both reads a table and calls the client, so it is deliberately not a pure
repository. That is correct here: the cache exists *only* to avoid a request, so a
module that knew about the table but not the request would be a seam in the wrong
place and the orchestration would leak upward anyway.

---

## 7. Licence and attribution

NEU-1145 §6 states the extraction is minimised to "**one integer per `(show,
season)`**" and that we "never copy TVmaze's dates, titles, numbering or any other
field". A cached show id is a second integer, per show, taken from TV Maze. **§6's
wording is amended by this ticket rather than left contradicting the code** — that
is AC 5.

**The amended position, in order of what it rests on:**

1. **The attribution condition is already satisfied.** TV Maze data is CC BY-SA 4.0
   and the credit was restored to the SPA footer by NEU-1145's frontend half. Even
   on the most conservative reading — that a share-alike obligation attaches to an
   identifier — we are compliant. Nothing turns on the next point.
2. **A show id is an identifier, not creative expression.** It is a bare fact about
   which record in a database corresponds to which series, carrying none of the
   authored content the licence exists to govern.
3. **The extraction is still minimised, and the principle still binds.** We store
   the offset per `(show, season)` and the show id per show, and nothing else — no
   dates, no titles, no numbering, no synopses, no images. The reason to keep
   minimising is not licence compliance alone: NEU-1146 deleted 782k rows because
   the operator objected to phantom rows in the catalog, and that objection is
   independent of any licence.

**The spine/sidecar distinction, made explicit.** §6 currently says "the catalog
itself remains free of TV Maze rows", which this is the second table to sit
awkwardly beside. The line actually drawn — by ADR-0012, by §2 above, and by
`air_date_offset` before either — is:

> `catalog.show`, `catalog.season` and `catalog.episode` hold no TV Maze-derived
> value at any grain. Sidecar tables in the `catalog` schema
> (`air_date_offset`, `airdate_show_state`) hold derived integers, and that is a
> different thing from the catalog holding TV Maze rows.

Making it explicit is what stops a later reader taking §6 as "no TV Maze value may
exist inside schema `catalog`" and either breaking the rule silently or being
blocked by it wrongly.

**Three further sites restate the old wording and are edited to match.** The AC
names only the spec; leaving these would leave the code contradicting it:

- `src/tvbf/airdates/client.py` — the module docstring's "the only value that
  survives a call is **one integer per `(show, season)`**", *and* its "**Two
  requests per show**" paragraph, which this ticket makes wrong.
- `src/tvbf/airdates/reconcile.py` — "Nothing else about TV Maze is stored (§6)".
- `CLAUDE.md` — the non-obvious-patterns entry ending "one integer per `(show,
  season)`, never TV Maze's dates, titles or numbering".

---

## 8. Operations

**The first run after deploy costs exactly what today costs.** The table is created
empty and nothing is backfilled, so every show takes a lookup plus an episode fetch
and the run takes ~23 minutes. The saving appears on the **second** run. AC 3's
"after" measurement must therefore come from a run at least one night past the
deploy, or the cold run reads as a failed optimisation.

**Measurement needs no new machinery.** `catalog.ingest_run` already carries
`started_at` / `finished_at` for `kind='airdate_reconcile'`, so the 2026-08-14
baseline is recorded in production and the after-number comes from the same query.
Poll or read a run via `task ingest:status -- <uuid>`.

**The escape hatch is a `DELETE`.** The table is a pure derived cache (§3), so
truncating it is always safe and the next run rebuilds it:

```sql
DELETE FROM catalog.airdate_show_state;                      -- full reset
DELETE FROM catalog.airdate_show_state WHERE tvmaze_id IS NULL;  -- re-ask the negatives
```

Documented here and in `docs/migration/README.md` beside the other airdate runbook
entries. **No CLI flag and no `task` target**: it matches how `air_date_offset`'s own
escape hatch works — a hand-entered `season_number IS NULL` row, no code — and
adding a flag would widen `jobs/scheduled.py`'s deliberately narrow shared shape for
something that should happen approximately never.

---

## 9. What the run reports

**Not-found shows keep logging identically**, at `log.info`, per show, whether the
conclusion came from a request or from a row. The log's subject is "which shows
cannot be corrected", which is unchanged by how we learned it; making its volume
depend on cache state would be the log reporting on our implementation instead of on
the data, and would make a before/after run diff incomparable.

**Three new counters on `ReconcileResult`**, all three added to the closing log line
beside the existing counts:

- `lookups_spent` — `/lookup/shows` requests actually made
- `links_reused` — resolved from a cached row, no request
- `links_invalidated` — §5's stale-id path

`lookups_spent` falling to near zero across consecutive runs *is* AC 1, observable in
production rather than only in a test.

---

## 10. Acceptance criteria

1. A show resolved once is not looked up again; the pass issues **one** TV Maze
   request per show in steady state.
2. A show TV Maze does not carry is not re-looked-up every night, and is still
   re-checked eventually — after `RELOOKUP_MISSING_AFTER` (§4).
3. A cached id that stops resolving is invalidated and re-resolved rather than
   silently ending the show's reconciliation (§5).
4. Measured before/after wall clock recorded on the ticket, the "after" taken from a
   run at least one night past deploy (§8). Expect ~23 min → ~12 min at today's work
   list.
5. The rate budget still sees every request. **Do not** reach for `follow_redirects`,
   which spends a request the limiter cannot see — that is NEU-1145's `_SHOW_PATH`
   fix and the reason the client parses `Location` instead.
6. `docs/specs/NEU-1145-airdates-one-day-early.md` §6 updated for the widened
   extraction, and the three further sites in §7 updated to match.
7. No test makes a live TV Maze call; `tests/unit/airdates/test_client.py` is the
   pattern.

---

## 11. Testing notes

- **`respx` unit test on the client**, pinning `get_show_episodes`'s new contract:
  404 → `None`, 200-with-`[]` → `[]`. `tests/unit/airdates/test_client.py` exists
  because its absence cost a production run; this is the second wire-level contract
  it protects.
- **Integration tests over `show_state`** for the three cache states of §4, the
  30-day expiry via an explicit override rather than a clock, and §5's
  invalidate-and-retry path driven directly rather than through a full pass.
- **A two-run integration test asserting `FakeOracle.lookups` stays flat on the
  second pass.** The counter already exists on the stub. This assertion *is* AC 1.
- Fixtures seeding `catalog` need `await session.flush()` between parent and child
  inserts — there are no `relationship()` declarations.
- Autouse fixtures must not request `monkeypatch`.
- New `result.rowcount` sites need `# type: ignore[attr-defined]`, and CLAUDE.md's
  list of such files needs updating (`grep -rln 'rowcount.*type: ignore' src/` is
  authoritative).
- Adding a table to `catalog` does not touch the five schema-enumeration sites —
  those are per *schema*, not per table — but CLAUDE.md's DB-topology section lists
  `catalog`'s tables and does need the new name, as does the module map for
  `airdates/show_state.py`.
- Run `task format` before committing; pre-commit checks formatting but does not fix
  it.

---

## 12. Out of scope

- **Scoping the work list to change** — NEU-1149. This ticket halves a number that
  still grows with catalog size and user count; that one makes the number bounded by
  change. NEU-1149's `last_reconciled_at` lands as a column on §3's table.
- **Periodically re-checking a resolved id** (§5, accepted and not designed for).
- **Storing the external ids used at resolution time** (§4.1, considered and
  rejected).
- **Widening the work list to the full catalog** — still out of scope, per NEU-1145
  §9, and this change does not alter the etiquette argument behind it.
