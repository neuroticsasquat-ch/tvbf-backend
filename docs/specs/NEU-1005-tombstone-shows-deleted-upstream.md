# NEU-1005 — Tombstone shows deleted upstream

**Ticket:** [NEU-1005](https://linear.app/neuroticsasquatch/issue/NEU-1005/backend-prune-shows-deleted-upstream-and-decide-what-happens-to-user)
**Related:** [NEU-967](https://linear.app/neuroticsasquatch/issue/NEU-967) (seasons, shipped), [NEU-1006](https://linear.app/neuroticsasquatch/issue/NEU-1006) (the abort threshold), [ADR-0004](../adr/0004-the-show-fetch-owns-the-season-set.md)
**Repo:** `tvbf-backend`

## Problem

NEU-967 made the show fetch authoritative for a show's **season** set. The level above is unhandled: a show TV Maze deletes outright stays mirrored forever and strands its seasons, which can never be stamped because the pass that would stamp them 404s on the parent.

Measured 2026-08-06 during NEU-967's part-2 cleanup: **58 shows returned 404** from `/shows/{id}`, holding 63 seasons — every remaining unstamped season in the catalogue. They were removed by hand, in two guarded transactions, to let the cleanup finish. `count(*) FROM tvmaze.season WHERE credits_synced_at IS NULL` reached 0 because of that manual surgery, not because code handled it. This ticket exists so that never has to happen again.

The population is **currently zero**, so this ticket needs no cleanup pass — only prevention.

## Decisions

### 1. Tombstone. Never delete.

`tvmaze.show` gains `deleted_upstream_at timestamptz NULL`. Nothing is ever `DELETE`d.

ADR-0004's "deletion is recoverable by construction" argument, which justified deleting seasons, **does not carry over**. A wrongly deleted season is re-created with the same upstream id by the next show fetch. A wrongly deleted `app.user_show_watch` row is gone — nothing upstream knows the user was tracking that show. That inverts the trade.

Two further findings, either of which would sink a hard delete on its own:

**A hard delete cannot even run in the general case.** `import_ne.show_resolution` references `tvmaze.show` with `confdeltype = 'a'` — **NO ACTION**, not cascade — and holds 522 rows. A referenced show raises an FK violation instead of deleting. The 58 manual deletes succeeded only because none happened to be referenced; that was luck, not design.

**`app.activity_event` has no foreign key at all.** It is polymorphic — `target_type` + `target_id`, with only an index (`app/models.py:289-303`) — and holds 741 rows. Deleting a show leaves activity events pointing at a target that no longer exists, silently, with nothing to catch it. Neither cascade nor NO ACTION applies.

For reference, the user data a cascade would reach:

| table | rows in prod | path |
| --- | ---: | --- |
| `app.user_episode_watch` | 8,492 | show → episode → watch |
| `app.user_show_watch` | 620 | direct |
| `app.user_episode_rating` | 72 | show → episode → rating |
| `app.user_show_rating` | 61 | direct |
| `app.activity_event` | 741 | **no FK — orphans silently** |

This also honours the standing rule that ingest must never destroy what users created in-app.

### 2. The signal is the `/updates/shows` reverse diff, floor-guarded

A show mirrored but **absent from `/updates/shows`** is deleted upstream. The feed is the authoritative full list of every show id upstream holds.

Validated empirically against prod on 2026-08-06, immediately after the manual cleanup:

```
feed entries                      88,997
mirrored shows                    88,971
in DB but NOT in feed                  0   ← exactly the 58 already removed, no more
in feed but NOT in DB                 26   ← ordinary ingest backlog
```

Zero false positives. The reverse diff identified precisely the set that hand-probing had found, which is as good as this evidence gets.

**The guard matters more than the signal.** The diff is `existing - set(updates)`, so a truncated or empty feed returning 200 would tombstone the entire catalogue — NEU-967's empty-embed footgun one level up. `get_show_updates` (`client.py:192-195`) does no sanity check today.

Refuse to tombstone unless the feed is plausible, on **both** an absolute and a relative floor:

```python
_MIN_FEED_ABSOLUTE = 50_000          # catches an empty or badly truncated 200
_MIN_FEED_RELATIVE = 0.95            # catches a partial feed near full size
```

If either fails: tombstone nothing, log an error, let the rest of the daily proceed. The failure mode is a skipped tombstone pass, not a wrecked catalogue. Both thresholds are stated as intent in the ADR so they don't read as magic numbers.

### 3. Tombstoned means hidden from discovery, not hidden from its owner

A tombstoned show disappears from browse and search — nobody can newly find or add one. For users already tracking it, **My Shows, Watch Next, Upcoming, watch history and ratings keep working unchanged**, and the show detail page stays reachable by id.

Losing a show off your list with no explanation is worse than the mirror carrying a dead row. This also keeps the filter surface small — the objection ADR-0004 raised against tombstoning ("a filter every future query has to remember") applies to roughly two discovery queries here, not to all ~28 functions in `browse_queries.py` plus the five app-side modules that touch shows.

## What to build

### 1. Column + migration

`tvmaze.show.deleted_upstream_at: timestamptz NULL`, plus an Alembic migration. Unlike NEU-967, this ticket **does** carry a migration. No `ck_ingest_run_kind` change — see below.

### 2. The tombstone pass, inside the daily

`run_update` already calls `client.get_show_updates()` (`update.py:34`) and holds the **full** feed in `updates` before filtering by cursor. So the reverse diff costs **zero extra requests**, needs no new `ingest_run` kind, no admin route, and no task target. It runs every day for free.

Placement: **after** the per-show loop completes normally. A run that aborts on consecutive failures returns early and therefore tombstones nothing — deliberately conservative, and it composes with NEU-1006 rather than depending on it.

Two symmetric writes:

- **Tombstone** — `deleted_upstream_at = now()` where the show is mirrored, absent from the feed, and not already tombstoned.
- **Resurrect** — `deleted_upstream_at = NULL` where a tombstoned show reappears in the feed. TV Maze does restore ids, and a tombstone that could never lift would be a one-way door. This is the cheap half of making the decision reversible.

### 3. The discovery filter

`WHERE show.deleted_upstream_at IS NULL` on the browse list path — `list_shows` and the AKA-aware search it delegates to (`browse_queries.py:328`, `430`). Everything else is left alone by decision 3: show detail by id, My Shows, Watch Next, Upcoming, watch tracking, person filmographies.

### 4. Documentation

- **ADR-0005**, *shows deleted upstream are tombstoned, not deleted* — amending ADR-0004, with a reciprocal `Amended by` marker added to 0004, following the corpus convention (0003 → 0001).
- **`CONTEXT.md`** gains **Tombstone**, and **Phantom record** is extended to note that seasons are pruned while shows are tombstoned — the asymmetry is deliberate and needs saying.
- **`CLAUDE.md`** (umbrella, *Non-obvious patterns*): the feed floor guard, and why a hard delete is not an option.

## Traps

**The feed is the whole catalogue every day.** The reverse diff must never run on a partial fetch. The floors are the only thing standing between a bad upstream response and 89k tombstoned shows.

**`import_ne.show_resolution` is NO ACTION.** Anyone who later "simplifies" this to a hard delete will discover it in production, on the shows that happen to be referenced. The ADR must say so explicitly.

**`activity_event` has no FK.** It cannot be relied on to protect itself, now or in any future deletion work.

**Tombstoning is not the same as `credits_synced_at IS NULL`.** A tombstoned show's seasons stay unstamped forever by design. Any future coverage check must exclude tombstoned shows or it will report a permanent, unfixable residue — this is exactly the gap that made NEU-967's acceptance criterion unreachable as written.

## Testing

Integration tests in `tests/integration/tvmaze/`:

1. A mirrored show absent from the feed is tombstoned; a show present in the feed is not.
2. A tombstoned show that reappears in the feed has `deleted_upstream_at` cleared.
3. Feed below the absolute floor → nothing tombstoned, error logged, the rest of the daily still runs.
4. Feed below the relative floor → same.
5. **No row is ever deleted** — assert show/season/episode counts are unchanged across a tombstoning run. This is the test that fails if someone reintroduces a delete.
6. A user's `user_show_watch`, ratings and episode-watch rows for a tombstoned show are untouched and still readable.
7. `list_shows` excludes tombstoned shows; `GET /shows/{id}` for one still returns 200.
8. A run that aborts on consecutive failures tombstones nothing.

## Acceptance

- A show absent from `/updates/shows` is tombstoned by the daily, with no extra upstream requests
- A tombstoned show reappearing upstream is resurrected
- An implausible feed tombstones nothing and says so loudly
- **No user's My Shows entry, rating, watch history or activity event is destroyed** — test 5 and 6 would fail if it were
- Tombstoned shows are absent from browse/search and still reachable by id
- ADR-0005 written, ADR-0004 back-linked, `CONTEXT.md` and `CLAUDE.md` updated
- `task lint`, `task typecheck`, `task test` green

## Out of scope

**A cleanup/backfill pass.** The reverse diff is 0 today — the population was cleared by hand. Prevention is the whole job.

**Episodes deleted upstream.** Same class one level down, and `app.user_episode_watch` FKs the episode directly, so it carries the same user-data hazard and deserves its own pass with its own measurement.

**Seasons.** Shipped in NEU-967.

**The consecutive-failure abort.** NEU-1006. Complementary and independent — this ticket reduces how often 404s are hit; that one stops a run dying when they are.
