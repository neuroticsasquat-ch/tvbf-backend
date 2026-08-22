# NEU-967 — Prune seasons deleted upstream, and stop the mirror re-accruing them

**Ticket:** [NEU-967](https://linear.app/neuroticsasquatch/issue/NEU-967/backend-prune-seasons-deleted-upstream-and-stop-the-mirror-re-accruing)
**Blocked by:** NEU-961 (the ~29h episode-credits pass — its completion defines this work's list)
**Related:** [ADR-0003](../adr/0003-episode-credits-are-fetched-per-season.md)
**Repo:** `tvbf-backend`

## Problem

`tvmaze.season` holds season records TV Maze has since deleted. `upsert_show_payload` upserts the seasons in a show fetch and never removes ones that have disappeared — there is no `delete(m.Season)` anywhere in `upsert.py` — so a season upstream creates and later removes stays in our mirror permanently.

The episode-credits pass surfaces them: `/seasons/{id}/episodes` 404s, the season's `credits_synced_at` stays NULL, and every subsequent run re-attempts it. Failures ran 0.6–1.1% of attempted seasons during NEU-961, projecting ~1,600 across the full 188k. Sampling confirmed `/seasons/{id}` **itself** 404s, so the season is genuinely gone rather than merely episode-less. The shape is consistent: high season ids (199k–203k, recently created) hanging off very low show ids — seasons added to legacy shows and later cleaned up. Show 71 holds two records both numbered 35, the duplicate-`(show_id, number)` quirk `CLAUDE.md` documents; upstream deduplicated them and we kept both.

A one-off cleanup is not enough on its own — the population regrows. This is two pieces of work, and doing the cleanup without the fix buys a clean number that decays again.

## What to build

### 1. Stop the leak — the show fetch becomes authoritative for a show's season set

`upsert_show_payload` gains an opt-in prune. Same argument ADR-0003 makes for a season response being authoritative for its credits, one level up.

```python
async def upsert_show_payload(
    session: AsyncSession,
    show: TVMazeShow,
    *,
    episodes: list[TVMazeEpisode] | None = None,
    prune_seasons: bool = False,
) -> int:
```

Order inside the function is load-bearing:

1. `upsert_show(...)`
2. upsert each season in `show.embedded.seasons`
3. **prune** — when `prune_seasons` is True: `DELETE FROM tvmaze.season WHERE show_id = :id AND id NOT IN (:payload_season_ids)`
4. `upsert_episodes(...)`

**The prune must sit between the season upsert and the episode upsert.** `upsert_episodes` resolves each episode's `season_id` by querying live season rows for the show and building `season_by_number` (`upsert.py:180-185`). Prune first and that map contains only surviving seasons — so in the duplicate-number case the survivor wins the lookup and the phantom's episodes are **re-pointed to it**, not nulled. Prune after, and the episodes are bound to a row that is about to disappear and get nulled by the FK instead. This is the difference between repairing show 71 and merely not-corrupting it.

**Diff by id, never by `(show_id, number)`.** The duplicate-number case is precisely where a number-keyed diff breaks: the correct outcome is "delete the id that is gone, keep the id that remains," and only an id-set diff expresses that.

**Callers opting in** — all four request the seasons embed today, so all four pass `prune_seasons=True`:

| caller | embed source |
| --- | --- |
| `ingest.run_initial_ingest` | `_DEFAULT_EMBEDS` = `("episodes", "seasons")` |
| `update.run_update` | `_DEFAULT_EMBEDS` |
| `show_refresh.run_show_refresh` | `_REFRESH_EMBEDS` = `["seasons", "cast", "crew"]` |
| `ratings_backfill` | explicit `embed=["episodes", "seasons"]` |

### 2. Clean up what has accrued — no new code

The mechanism is the prune from part 1, delivered through the **existing** `POST /admin/refresh-shows` pass. Reset the pass-A watermark for the affected shows and trigger it:

```sql
UPDATE tvmaze.show SET credits_synced_at = NULL
WHERE id IN (SELECT DISTINCT show_id FROM tvmaze.season WHERE credits_synced_at IS NULL);
```

`run_show_refresh` then fetches each of those shows with `embed[]=seasons`, calls `upsert_show_payload` (which now prunes), re-stamps the watermark, and calls `refresh_show_season_credits` for the surviving seasons. That last step matters: it re-fetches credits for seasons that were unstamped for *transient* reasons, so one operation resolves both populations — phantoms deleted, transients stamped — and `count(*) WHERE credits_synced_at IS NULL` reaches 0 without a second pass.

Rejected alternatives: a bespoke 404-probe script over the ~1,600 unstamped seasons (a second deletion codepath existing only for one run, and it heals nothing else), and a dedicated admin route with its own `ingest_run` kind (a migration to extend `ck_ingest_run_kind`, plus routes, task targets and tests, for something that runs once).

### 3. Documentation

- **ADR-0004**, short: *the show fetch is authoritative for a show's season set*. It records the stance, the opt-in guard and why the mirror deletes rather than tombstones — and gives the sequel (episodes deleted upstream) something to amend.
- **`CONTEXT.md`** gains **Phantom record** under *The catalog*: a mirrored row for an entity upstream has deleted, distinguishable only by the fetch that names its parent. _Avoid_: orphan, stale row.
- **`CLAUDE.md`** (umbrella, *Non-obvious patterns*): the `prune_seasons` opt-in and why it is not implicit.

## Decisions

**Delete the rows; do not tombstone.** The row describes something that does not exist; `ingest_run` already records how many failed; and a filter every future season query must remember is exactly the kind of thing that gets forgotten.

The decisive argument is that **deletion here is recoverable by construction**. If a season is ever wrongly deleted, the next show fetch re-creates it with the same upstream id, its `credits_synced_at` comes back NULL, and the credits backfill re-fetches it. Nothing unrecoverable is lost: `season.credits_synced_at` is derived state, and `episode.season_id` is re-resolved from the number map on the next show fetch. That reduces the blast radius of a mistake to "one show is briefly missing season metadata", which does not justify a migration and a permanent query filter.

**The prune is opt-in per caller, not inferred from the payload.** `TVMazeEmbedded.seasons` defaults to `[]` (`api_payloads.py:217`), so `upsert_show_payload` cannot tell "no seasons upstream" from "the caller didn't request the embed". `get_show` explicitly supports `embed=[]` (`client.py:153-162`) and a unit test exercises it (`tests/unit/tvmaze/test_client.py:65`). Every caller requests seasons today, which is what makes an unguarded prune safe *now* and dangerous *later*: a future caller trimming its embeds would silently wipe every season of every show it touches.

The guard must **not** be an implicit `if not seasons: skip` — a show legitimately having zero seasons exists, and that guard conflates it with the missing-embed case, reintroducing the leak for exactly the shows where pruning matters. With the explicit flag, an empty list under `prune_seasons=True` means an authoritative zero and deletes all of that show's seasons. Assert that in a test so it reads as intended rather than as an accident.

**Blast radius, verified.** Exactly one FK points at `tvmaze.season`: `episode.season_id`, `ON DELETE SET NULL` (`confdeltype = 'n'`). No `app.*` table references a season, so no user data is touched. Orphaned episodes stay browsable — the browse layer filters on `episode.season`, the integer, not `season_id`. Four of the five sampled phantoms had zero mirrored episodes; 199285 had three.

## Traps

**The stamped-phantom tail.** Any work list keyed on `credits_synced_at IS NULL` is incomplete by construction. A season deleted upstream *after* its credits were successfully fetched carries a stamp and is invisible to that query; it is pruned only when its show is next fetched by the daily. So `unstamped = 0` and a PASS from `verify_episode_credits.sh` measure **coverage**, not absence of phantoms. Both are honest signals; neither asserts the stronger claim. The window is small today because NEU-961 just ran, it widens over time, and only a full show-refresh pass ever closes it completely.

**Rate-limiter contention.** `get_rate_limiter` is `@cache`d and process-wide (NEU-955). The cleanup run shares the 18 req/10s budget with the daily and with any in-flight credits backfill — concurrent jobs do not double the rate, they just each go slower. Run the cleanup when nothing else is going.

## Testing

Integration tests against the seeded DB, in `tests/integration/tvmaze/`:

1. A season absent from the payload is deleted when `prune_seasons=True`; seasons present in the payload survive.
2. `prune_seasons=False` (the default) deletes nothing, even when the payload carries no seasons at all.
3. **Duplicate-number repair** — two mirrored seasons numbered 35, payload names only one: the phantom is deleted *and* its episodes' `season_id` now points at the survivor. This is the test that pins the prune-before-episodes ordering; it fails if the two steps are swapped.
4. No surviving season of that number: the episodes' `season_id` is NULL and they remain queryable by `episode.season`.
5. An empty `embedded.seasons` under `prune_seasons=True` deletes every season of that show (authoritative-zero, asserted deliberately).
6. The daily path end-to-end: `run_update` over a show whose payload has dropped a season leaves the mirror matching the payload.

No migration, so no alembic work and no `ck_ingest_run_kind` edit.

## Runbook

Sequenced after NEU-961's prod run completes.

1. Merge and deploy part 1.
2. Optionally suspend the daily update in Coolify (→ tvbf-backend → Scheduled Tasks → "daily-update") so the cleanup has the request budget to itself. Optional since NEU-1008: the budget is shared across processes, so a concurrent daily costs wall-clock, not correctness.
3. Record the baseline: `SELECT count(*) FROM tvmaze.season WHERE credits_synced_at IS NULL;` and `SELECT count(DISTINCT show_id) FROM tvmaze.season WHERE credits_synced_at IS NULL;`
4. Run the watermark reset from §2.
5. `POST /admin/refresh-shows`; record the `run_id`. Poll `GET /admin/refresh-shows/{run_id}`. Expect minutes, not hours — the work list is the affected shows plus the ~78 of 89,012 already unstamped from pass A.
6. Confirm `SELECT count(*) FROM tvmaze.season WHERE credits_synced_at IS NULL` → 0.
7. `scripts/verify_episode_credits.sh prod` → PASS on coverage.
8. Resume the daily update in Coolify if you suspended it at step 2.

## Acceptance

- A season absent from a show's `embed[]=seasons` response is deleted by the show fetch; an integration test covers it
- The prune is opt-in per caller — a caller that did not request the embed deletes nothing; a test covers it
- Episodes of a pruned season are re-pointed to a surviving same-numbered season where one exists, and otherwise keep `season_id` NULL and stay reachable by season number; tests cover both
- All four `upsert_show_payload` callers pass `prune_seasons=True`
- The existing *unstamped* phantom population is cleared; `SELECT count(*) FROM tvmaze.season WHERE credits_synced_at IS NULL` reads 0 in prod. Phantoms already stamped are out of reach of that query by construction and age out via the daily
- Re-running the episode-credits backfill afterwards finds no work
- `scripts/verify_episode_credits.sh prod` reports PASS on coverage
- ADR-0004 written; `CONTEXT.md` and `CLAUDE.md` updated
- `task lint`, `task typecheck`, `task test` green

## Out of scope

**Episodes deleted upstream.** The same class of problem one level down. The credits pass does not surface them the way it surfaces seasons, so there is no measurement to act on yet — worth its own ticket once there is.

**A full-catalogue sweep for stamped phantoms.** Only a complete show-refresh pass (~87k shows, ~27h) finds those, and the daily retires them for free over time. Not worth 27 hours of rate-limit budget today.

**Shows deleted upstream.** A show that 404s takes its seasons with it in our mirror and is not handled here; the cleanup run will simply fail that show.
