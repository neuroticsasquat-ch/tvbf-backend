# The show fetch is authoritative for a show's season set

**Status:** accepted (2026-08-06)
**Extends:** [ADR-0003](./0003-episode-credits-are-fetched-per-season.md)
**Amended by:** [ADR-0005](./0005-shows-deleted-upstream-are-tombstoned.md)

> The delete-don't-tombstone decision below is scoped to **seasons** and does not generalise upward. Shows deleted upstream are *tombstoned*, because `app.user_show_watch` and `app.user_show_rating` cascade from `tvmaze.show` — so the "deletion is recoverable by construction" argument fails, a hard delete cannot even run when `import_ne.show_resolution` references the row, and `app.activity_event` would orphan silently. See ADR-0005.

A show payload fetched with `embed[]=seasons` owns that show's season set. Seasons the payload does not name are deleted from the mirror. This is ADR-0003's argument — a season response is authoritative for every credit on every episode it contains — applied one level up.

Before this, `upsert_show_payload` upserted the seasons in a fetch and never removed any: there was no `delete(m.Season)` in `upsert.py` at all. A season TV Maze created and later deleted stayed in the mirror permanently, and nothing ever noticed.

The episode-credits pass is what surfaced them. `/seasons/{id}/episodes` 404s on such a season, so its `credits_synced_at` stays NULL and every subsequent run re-attempts it forever. NEU-961's prod run failed 245 seasons, **every one a 404**, spread evenly across 29 hours rather than clustered like an outage. Sampling confirmed `/seasons/{id}` *itself* 404s, so the season is genuinely gone upstream rather than merely episode-less. The shape was consistent: recently-created season ids (199k–203k) hanging off very low show ids — seasons added to legacy shows and later cleaned up.

## Delete, don't tombstone

The alternative was a `deleted_upstream_at` column that the backfill work list and the verify script both exclude.

We delete. The row describes something that does not exist; `ingest_run` already records how many failed; and a filter every future season query must remember is exactly the kind of thing that gets forgotten.

The decisive argument is that **deletion here is recoverable by construction**. A wrongly deleted season is re-created with the same upstream id by the next show fetch, comes back with `credits_synced_at` NULL, and is re-fetched by the credits backfill. Nothing unrecoverable is lost: `season.credits_synced_at` is derived state, and `episode.season_id` is re-resolved from the number map on the next show fetch. That bounds the blast radius of a mistake to "one show is briefly missing season metadata", which does not justify a migration plus a permanent query filter.

Blast radius, verified: exactly one FK points at `tvmaze.season` — `episode.season_id`, `ON DELETE SET NULL`. No `app.*` table references a season, so no user data is touched. Episodes of a pruned season stay browsable, because the browse layer filters on `episode.season`, the integer, not `season_id`.

## The prune is opt-in per caller

`prune_seasons` defaults to False and each caller passes True explicitly.

This looks like ceremony and is not. `TVMazeEmbedded.seasons` defaults to `[]`, so `upsert_show_payload` cannot distinguish "this show has no seasons upstream" from "the caller didn't request the seasons embed" — and `get_show` explicitly supports `embed=[]`. All four callers request seasons today, which is exactly what makes an unguarded prune safe *now* and dangerous *later*: a future caller that trims its embeds would silently wipe every season of every show it touches.

The guard must **not** be an implicit `if not seasons: skip`. A show legitimately having zero seasons exists, and that guard conflates it with the missing-embed case — reintroducing the leak for precisely the shows where pruning matters. With an explicit flag, an empty list under `prune_seasons=True` means an authoritative zero and deletes all of that show's seasons. `test_empty_payload_seasons_under_prune_is_an_authoritative_zero` asserts that deliberately, so it reads as intended rather than as an accident.

## Ordering is load-bearing

The prune runs **between** the season upsert and the episode upsert.

`upsert_episodes` resolves each episode's `season_id` from a live query over the show's seasons, building a `{number: id}` map. Pruning first leaves only survivors in that map, so in the duplicate-number case — two mirrored seasons numbered 35, upstream having deduplicated them — the survivor wins the lookup and the phantom's episodes are **re-pointed onto it**. Prune afterwards and those episodes are bound to a row about to disappear, then nulled by the FK. That is the difference between repairing the show and merely not corrupting it.

Diff by id, never by `(show_id, number)`. The duplicate-number case is precisely where a number-keyed diff breaks: the correct outcome is "delete the id that is gone, keep the id that remains", and only an id-set diff expresses that.

`test_prune_runs_before_episodes_are_written` pins the ordering by spying on what `upsert_episodes` can see when it runs. It deliberately does **not** assert an outcome: with both duplicates present, `season_by_number` is a dict comprehension over an unordered `SELECT`, and which duplicate wins depends on heap order — which `upsert_season`'s `ON CONFLICT DO UPDATE` perturbs via MVCC. An outcome-based ordering test passes with the two steps swapped and proves nothing; this was observed directly while writing it.

## Consequences

> **Note (NEU-1050).** This ADR is a historical record. The TV Maze ingest it
> describes was retired, along with `scripts/verify_episode_credits.sh`,
> `POST /admin/refresh-shows` and the daily update named below. The decision it
> records is still live: `tmdb/upsert.py:upsert_series_payload` carries the same
> opt-in `prune_seasons`, for the same reason.

**A work list keyed on `credits_synced_at IS NULL` is incomplete by construction.** A season deleted upstream *after* its credits were fetched carries a stamp and is invisible to that query; it is pruned only when its show is next fetched by the daily. So `unstamped = 0` and a PASS from `verify_episode_credits.sh` measure **coverage**, not absence of phantoms. Both are honest signals; neither asserts the stronger claim. The window is small immediately after a credits pass, widens over time, and only a full show-refresh pass ever closes it completely.

**The cleanup needs no new code.** Resetting `show.credits_synced_at` for the affected shows and running the existing `POST /admin/refresh-shows` deletes the phantoms via this prune and re-fetches credits for seasons that were unstamped for merely transient reasons — so one operation resolves both populations. A bespoke 404-probe script was rejected as a second deletion codepath existing for one run that heals nothing else.

**Episodes deleted upstream are the same problem one level down**, and are not addressed here. The credits pass does not surface them the way it surfaces seasons, so there is no measurement to act on yet. This ADR is the thing that sequel amends.
