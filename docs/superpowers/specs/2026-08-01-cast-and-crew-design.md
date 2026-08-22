# Cast and Crew — Design

**Date:** 2026-08-01
**Status:** Proposed
**Owner:** Tom
**Linear:** NEU-924 (project: TVBF: Cast and Crew)

## Problem

The catalog mirror knows what shows exist, when they aired, and how they're rated. It knows nothing about **who made them**. There is no way to see who stars in a show, who directed it, who guest-starred in an episode, or what else an actor has been in.

This is the last major axis of the TV Maze catalog we don't mirror, and it's the one users ask for by name.

## Goal

Mirror TV Maze's people data as a first-class axis of the catalog, and surface it:

- Show pages list cast (person + character, in billing order) and crew (person + role).
- Episode pages list guest cast.
- Person pages show who someone is and an itemized, complete filmography — main roles, crew credits, and guest appearances.
- Search returns people as well as shows.

**Reliability over time is a first-class goal, not a nice-to-have.** A person's name can change (Elliott Page), and a credit that was true yesterday must still be findable tomorrow. The mirror is expected to stay correct without manual intervention.

## Non-goals

- **Replicating IMDb.** No trivia, no filmography ratings, no "known for" heuristics, no episode-level crew.
- **Character search.** Characters get their own table because credits reference them, but `GET /people?search=` matches `person.name` only.
- **Cross-entity search.** "Shows with X in them" is answered by clicking through to the person page, not by folding person names into show search. See [Search](#search).
- **Mirroring images.** Person headshots are still hot-linked to TV Maze CDN URLs, same as show images today. Mirroring image bytes locally is wanted eventually but is out of scope here.
- **Splitting the mirrors into standalone read-only data services** with their own fetch services. This is a real architectural question and it is explicitly parked — nothing in this design depends on the answer.

## Verified upstream facts

Everything below was confirmed against the live TV Maze API on 2026-08-01. Several of these contradict assumptions in the original tickets, so they're recorded rather than left implicit.

### Endpoints and payload shapes

| Fact | Detail |
|---|---|
| Four embeds combine on one show request | `/shows/{id}?embed[]=episodes&embed[]=seasons&embed[]=cast&embed[]=crew` → 200, all four present |
| The show record carries what three separate passes would fetch | `externals.thetvdb`, `rating.average`, seasons, episodes, cast, crew — one request |
| Cast entry shape | `{ person, character, self, voice }` — **no row id** |
| Crew entry shape | `{ type, person }` — **no row id**, `type` is free text |
| The embedded `person` object is complete | id, name, country, birthday, deathday, gender, image, updated — same shape as `/people/{id}` |
| `character` is never null on a cast entry | 286/286 sampled, including pure "self" appearances (Zachary Levi is credited as character 115733, "Zachary Levi") |
| `/characters/{id}` has **no** show link | Character↔show is only expressible through credit rows |
| **`specials=1` is ignored on the embed** | `/shows/168?embed[]=episodes&specials=1` → 91 episodes, 0 specials. Only `/shows/168/episodes?specials=1` returns them (92, 1 unnumbered) |
| Guest cast is per-episode only from the show side | `/shows/{id}/episodes?embed[]=guestcast` → `_embedded: []`; `/shows/{id}?embed[]=episodesguestcast` → 400 |
| **Guest cast is one request per _person_** | `/people/{id}?embed[]=castcredits&embed[]=crewcredits&embed[]=guestcastcredits` → all three, 16.9 KB for a person with 72 credits |
| There is **no season-level cast** | `/seasons/{id}/cast` and `/seasons/{id}/crew` → 404. Show and episode are the only grains |
| Show cast has a meaningful order | Documented as "ordered by importance, determined by the total number of appearances of the given character in this show." **Person-side credits carry no ordering** — billing order is only obtainable from the show side |

### Freshness

The TV Maze docs state that a person is marked updated "when any of their attributes are changed, but also when a cast- or crew-credit that involves them is created or deleted." They are silent on whether credit changes cascade to `/updates/shows`. Measured directly:

- Sampling people updated in the last 24h and checking whether their credited shows also appear in the 24h show feed: **17.95% vs. a 0.44% baseline — 40× enrichment.**
- Stripping the "actively-airing show" confound: **Feuten** (ended 2013-10-21) appears in the show feed with its person's bump **35 seconds apart**, and three other hits have **|Δ| = 0 seconds** — one write transaction touching both records.

**Conclusion: credit changes do cascade into `/updates/shows`.** The existing daily delta will keep cast and crew current for free once it embeds them, at zero marginal request cost.

The gap: a person's own attributes (name, headshot, death date) bump `/updates/people` without necessarily touching any show. That is what the person delta is for.

### Volume

Two early estimates were wrong and are recorded here so they don't get re-derived. A 19-famous-shows sample over-counted by ~3×; a 40-random-people sample under-counted badly because the distribution is heavily skewed and it contained only 8 cast credits. The 45-random-show sample (328 cast observations, 660 crew) is the one to trust.

| Table | Estimated rows | Basis |
|---|---|---|
| `person` | ~487k | exact — `/updates/people` returns 486,593 |
| `show_cast` | ~640k | 45 random shows: 7.29/show |
| `show_crew` | ~1.3M | 45 random shows: 14.67/show |
| `episode_guest_cast` | ~1.2M | 40 random people: 2.50/person (wide CI) |
| `character` | ~1M | roughly one per distinct credit |
| **total** | **~4.6M** | |

**This is comparable to the existing `tvmaze.episode` table (3.4M rows), not larger.** The project description's claim that this is "the largest catalog expansion since the original ingest" is false by row count and should be corrected. The expense is wall-clock request budget, not storage or query cost.

Sparsity is the norm and the UI must assume it:

- **27% of shows have zero cast entries.**
- **96% of episodes have zero guest cast.**
- The tail is long: The Simpsons has **1,420** cast rows and 533 crew rows.

Crew types are a controlled-ish vocabulary: 100 distinct values across 1,530 crew rows from 20 shows, heavily concentrated (Executive Producer ×149, Co-Executive Producer ×126), 26 singletons, longest string 27 chars.

## Architecture

### Two ingest axes

`tvmaze` stops being "the show mirror" and becomes a catalog mirror with **two independent ingest axes**, each with its own full-list feed, watermark, initial ingest, and daily delta.

```
show axis                                person axis
  /updates/shows  (87,395)                 /updates/people  (486,593)
  initial ingest  (ingest.py)              initial ingest   (person_ingest.py)
  daily delta     (update.py)              daily delta      (person_update.py)
  owns: show, season, episode,             owns: person, episode_guest_cast
        show_cast, show_crew,
        character, crew_role
```

See [ADR-0001](../../adr/0001-tvmaze-second-ingest-axis.md).

**Ownership is strict and the grains differ.** This is the single easiest thing to get wrong:

- `show_cast` / `show_crew` are owned by the **show** axis. Refresh grain is `WHERE show_id = ?`.
- `episode_guest_cast` is owned by the **person** axis, because guest credits are only reachable per-person. Refresh grain is `WHERE person_id = ?` — *not* per-episode.
- `person` rows are **created** by either axis (the show axis gets complete person objects embedded in cast/crew) but **credits** for a person are only ever written by the person axis.
- The person axis deliberately **ignores** the `castcredits` and `crewcredits` embeds even though it receives them, because only the show side carries billing order. Requesting them costs nothing extra; writing them would clobber `sort_order`.

### Refresh semantics: snapshot, not tombstone

Credit rows use **delete-then-insert**, exactly mirroring `upsert_akas` (upsert.py:218): surrogate `BIGSERIAL` PK, **no unique constraint**, `DELETE WHERE <owner_id> = ?` followed by one bulk insert in array order.

This was considered carefully against a tombstoning alternative (`first_seen_at` / `last_seen_at` / `removed_at` on a natural key), because "an actor who was ever on a show should stay surfaceable" is a stated goal. Snapshot wins because:

- **Upstream never prunes.** Grey's Anatomy's 40-entry cast still lists Patrick Dempsey (killed off in 2015), Sandra Oh, Katherine Heigl, T.R. Knight and Isaiah Washington. ER, ended in 2009, still lists all 26 including George Clooney. Doctor Who lists every Doctor. **Show cast is already cumulative across a show's whole life** — a snapshot of it loses nothing.
- A removal in the feed therefore almost certainly means a *correction* (bad entry, merged duplicate person), which is exactly what we want to propagate.
- No unique constraint means no repeat of the `tvmaze.season` trap, where a plausible-looking uniqueness assumption aborted ingestion on real upstream data.

Retained history was not free: it would have required a natural unique key, and the protection would only ever engage for the ~300 shows/day the delta revisits.

**Ordering is explicit.** Both credit tables carry a `sort_order` integer populated from the upstream array index. Relying on `BIGSERIAL` order would work today and break silently the first time an insert is batched or reordered.

### Data model

All tables live in the `tvmaze` schema. No `relationship()` declarations, per repo convention.

#### `person`

```
id                 INT      PK, no autoincrement (upstream id)
name               TEXT     NOT NULL
country_code       TEXT
country_name       TEXT
timezone           TEXT
birthday           DATE                    -- OptionalDate
deathday           DATE                    -- OptionalDate
gender             TEXT
image_medium       TEXT
image_original     TEXT
tvmaze_updated     BIGINT   NOT NULL
ingested_at        TIMESTAMPTZ NOT NULL DEFAULT now()
credits_synced_at  TIMESTAMPTZ             -- pass C watermark
```

`country` is flattened to `country_code` / `country_name` / `timezone`, matching `Network` and `WebChannel` exactly, rather than becoming a fourth lookup table.

`birthday` and `deathday` **must** use the `OptionalDate` alias from `api_payloads.py`. TV Maze returns `""` rather than `null` for unknown dates; a bare `date | None` will fail on real data.

#### `character`

```
id              INT   PK, no autoincrement (upstream id)
name            TEXT  NOT NULL
image_medium    TEXT
image_original  TEXT
```

No `show_id`: upstream provides none, and the relationship is already expressed by credit rows. Character identity is genuinely real and not derivable from the credit — The Simpsons credits both Hank Azaria and Harry Shearer as Carl Carlson, and both Nancy Cartwright and Yeardley Smith as Maggie.

#### `crew_role`

```
id    INT   PK, autoincrement (local id — upstream gives a bare string)
name  TEXT  NOT NULL, UNIQUE (uq_crew_role_name)
```

Modeled on `Genre`, which solves the identical problem. Resolve-or-insert during ingest, cached in memory for the duration of a run.

#### `show_cast`

```
id            BIGSERIAL  PK
show_id       INT        NOT NULL  FK -> show.id ON DELETE CASCADE
person_id     INT        NOT NULL  FK -> person.id
character_id  INT        NOT NULL  FK -> character.id
is_self       BOOLEAN    NOT NULL DEFAULT false
is_voice      BOOLEAN    NOT NULL DEFAULT false
sort_order    INT        NOT NULL

INDEX ix_show_cast_show_id_sort (show_id, sort_order)
INDEX ix_show_cast_person_id    (person_id)
```

No unique constraint — deliberate, see above.

#### `show_crew`

```
id          BIGSERIAL  PK
show_id     INT        NOT NULL  FK -> show.id ON DELETE CASCADE
person_id   INT        NOT NULL  FK -> person.id
role_id     INT        NOT NULL  FK -> crew_role.id
sort_order  INT        NOT NULL

INDEX ix_show_crew_show_id_sort (show_id, sort_order)
INDEX ix_show_crew_person_id    (person_id)
```

#### `episode_guest_cast`

```
id            BIGSERIAL  PK
episode_id    INT        NOT NULL  FK -> episode.id ON DELETE CASCADE
person_id     INT        NOT NULL  FK -> person.id
character_id  INT        NOT NULL  FK -> character.id
is_self       BOOLEAN    NOT NULL DEFAULT false
is_voice      BOOLEAN    NOT NULL DEFAULT false
sort_order    INT        NOT NULL

INDEX ix_egc_episode_id_sort (episode_id, sort_order)
INDEX ix_egc_person_id       (person_id)
```

The FK to `episode.id` is the reason specials must land before pass C — see [Sequencing](#sequencing).

#### Changes to existing tables

```
show.credits_synced_at   TIMESTAMPTZ    -- pass A watermark

ingest_run.kind          String(16) -> String(32)
                         CHECK rewritten to add:
                           'show_refresh', 'person_initial', 'person_update'
```

**The `ingest_run.kind` widening is not optional.** The column is `String(16)` today with `CHECK kind IN ('initial','update','akas_backfill','ratings_backfill')`. Every new kind needs a migration that both widens the column and rewrites the constraint. `'credits_backfill'` happens to be exactly 16 characters and would fit by luck; widening to 32 removes the trap for the next person.

#### Search index

```
CREATE INDEX ix_person_name_folded_trgm ON tvmaze.person
  USING gin (immutable_unaccent(lower(regexp_replace(name,
    '[[:punct:][:space:]]+', '', 'g'))) gin_trgm_ops);
```

Identical in shape to the existing `ix_show_name_folded_trgm`. `pg_trgm` and `unaccent` are already installed.

### Ingest

#### Pass A — show refresh (milestone 1)

**Two requests per show, ~27 hours** at 18 req / 10 sec over ~87,395 shows.

1. `GET /shows/{id}?embed[]=seasons&embed[]=episodes&embed[]=cast&embed[]=crew`
2. `GET /shows/{id}/episodes?specials=1`

Per show, in one transaction: upsert the show record (recovering `externals_tvdb` and `rating.average`), upsert seasons, upsert the union of episodes from both calls (specials included), upsert persons and characters and crew roles, then delete-then-insert `show_cast` and `show_crew`. Stamp `credits_synced_at`, and also stamp `ratings_synced_at` so a standalone ratings backfill becomes a no-op.

Orchestrator follows `akas_backfill.py` / `ratings_backfill.py`: todo list is `WHERE credits_synced_at IS NULL ORDER BY id`, each show in its own transaction so a crash leaves earlier shows done, per-show failures non-fatal and counted, abort after `INGEST_CONSECUTIVE_FAILURE_THRESHOLD` consecutive failures. `ingest_run.kind = 'show_refresh'`.

This pass carries four riders that would otherwise each be their own full-catalog pass:

| Rider | Would cost standalone |
|---|---|
| cast + crew | — (the point of the pass) |
| `externals_tvdb` recovery (NEU-922's backfill) | 13.5h |
| `rating.average` (NEU-161's unrun backfill) | 13.5h |
| specials (NEU-933's backfill) | 13.5h |

Specials cost +13.5h because they can't ride the embed. They're carried anyway — see [Sequencing](#sequencing).

#### Pass C — person initial ingest (milestone 2)

**One request per person, ~75 hours** over 486,593 people.

`GET /people/{id}?embed[]=guestcastcredits`

Per person, in one transaction: upsert the person record, upsert characters referenced by the guest credits, then delete-then-insert `episode_guest_cast WHERE person_id = ?`. Stamp `credits_synced_at`.

Todo list is every id in `/updates/people` whose `person.credits_synced_at IS NULL` — which correctly picks up persons already created by pass A with no credits fetched yet. `ingest_run.kind = 'person_initial'`.

Guest credits referencing an episode not in the mirror are the reason specials come first. Measured at **5.7%** of referenced episodes missing (39 of 679 sampled), closely matching NEU-933's 6.2% specials rate.

#### Daily deltas

**Show delta (existing, extended):** `update.py` adds `embed[]=cast&embed[]=crew` to the show fetch it already makes and a second `?specials=1` call. Because credit changes cascade into `/updates/shows` (measured above), cast and crew stay current with **no new requests** beyond the specials call — the delta already re-fetches these shows.

**Person delta (new):** reads the most recent succeeded `person_update` run's `last_update_cursor`, calls `/updates/people`, fetches persons whose epoch exceeds the cursor, advances the cursor.

⚠️ **`get_last_successful_cursor` was not axis-aware** — it returned the newest succeeded run with a non-null cursor regardless of `kind`. Both deltas store their watermark in that one column, so the show delta and person delta would have read each other's cursor and silently skipped work. **Fixed in NEU-954** (shipped ahead of this project).

The fix is scoped by *axis*, not by a single kind, and the distinction is load-bearing: `ingest.py` finalizes a succeeded `initial` run **with** a cursor, which the first daily delta inherits because it has no predecessor of its own. Narrowing the lookup to `kind == "update"` would break that handoff — the first delta after any initial ingest falls back to `0` and re-fetches the entire ~87k-show catalog, silently. `runs.py` now exposes `SHOW_CURSOR_KINDS = ("initial", "update")` and `PERSON_CURSOR_KINDS = ("person_initial", "person_update")`; the person axis passes the latter. **926 people changed in the sampled 24h ≈ 8.6 minutes of budget**, against the ~3 minutes the show delta uses today. This is what makes an Elliott Page rename propagate.

#### Batching

`upsert_episodes` chunks at `_EPISODE_BATCH_SIZE = 1000` because Postgres caps bind parameters at 32,767. The same ceiling applies here and the headroom is thinner than it looks: The Simpsons' 1,420 cast rows at ~6 bind params each is 8,520, and that lands in the same transaction as its person, character and crew-role upserts. Every bulk credit insert gets its own batch constant.

#### Admin surface

Following the existing pattern in `routers/admin.py`, all bearer-token protected:

- `POST /admin/refresh-shows` + `GET /admin/refresh-shows/{run_id}`
- `POST /admin/ingest-people` + `GET /admin/ingest-people/{run_id}`
- `POST /admin/update-people`

Plus `task` targets mirroring `task akas:backfill` / `task akas:backfill:status`.

### Backend API

New routes on the browse router — user-gated via `get_current_user`, CORS allowlisted, and taking the router-level cache dependency.

**Cache header, corrected:** `routers/browse.py` sets `private, max-age=300` via `_set_browse_cache`, *not* the `public, max-age=300` that CLAUDE.md documents — deliberately, because browse is session-gated and a shared cache must not fan one user's response out to others. Separately, `/shows` and `/shows/{id}` override to `private, no-store` because they carry per-user `my_rating`. **Credit and person routes carry no per-user fields, so they take the router default and add no override.**

**Milestone 1**

- `GET /shows/{id}/cast` → `[{ person, character, self, voice }]`, ordered by `sort_order`
- `GET /shows/{id}/crew` → `[{ person, role }]`, ordered by `sort_order`

Deliberately **not** embedded in `GET /shows/{id}`. That route already embeds seasons, and adding an unbounded cast list would put a 1,420-entry payload on the show detail response for The Simpsons.

**Milestone 2**

- `GET /people?search=&page=&per_page=` → paginated person list. Token-AND folded trigram match on `person.name`, reusing the `_fold` expression from `browse_queries.py` so both sides normalize identically.
- `GET /people/{id}` → person detail
- `GET /people/{id}/credits` → `{ cast: [...], crew: [...], guest_cast: [...] }`, each entry carrying enough show/episode context to link into the catalog
- `GET /episodes/{id}/guest-cast` → `[{ person, character, self, voice }]`

### Search

Person search is a **separate entity search**, not a third OR branch in show search.

An AKA *is a name of the show*, which is why OR-ing `show_aka.name` into title search is semantically correct and why the matched-AKA badge reads naturally. A cast member's name is not a name of the show. Folding ~1.3M crew names into the same predicate would mean `michael` or `smith` returns most of the catalog, and the existing token-AND semantics make it worse: `david lynch` would match any show with a David *and* a Lynch anywhere in its crew.

**The person page is the "shows with X in them" feature.** Search "Zachary Levi" → person result → his page lists his shows. This needs no new query shape — it's the same shape pointed at a different table.

### Frontend

**Milestone 1** — `ShowDetailPage` gains cast and crew sections. New components: `CastList`, `CrewList`, `PersonChip` (headshot + name, links to the person page once milestone 2 lands; plain text before then). Both sections must render an empty state gracefully — **27% of shows have no cast at all**.

**Milestone 2** — new `/people/:id` route and `PersonPage` with grouped credits (main roles, crew, guest appearances). `EpisodePage` gains a guest cast section, empty for 96% of episodes. `SearchOverlay` becomes multi-entity: a People section alongside Shows, with keyboard navigation across sections and a per-section empty state.

New API hooks in `src/api/shows.ts` and a new `src/api/people.ts`, going through `client.ts` as always.

## Sequencing

```
NEU-922 (thetvdb alias fix)
NEU-933a (specials fetch fix — ongoing path)
credit table definitions + migrations
        │
        ▼
   pass A — show refresh, 27h ──────► milestone 1 ships: show-page credits
        │
        ▼
   person subsystem (models, ingest, delta, admin)
        │
        ▼
   pass C — person ingest, 75h ─────► milestone 2 ships: person pages,
                                       guest cast, person search
```

**Total ~102 hours ≈ 4.3 days of rate-limited fetching, none of it overlappable** — every TV Maze job shares one rate-limit budget.

Three ordering constraints, all of the form "do the cheap thing before the expensive one":

1. **NEU-922 before pass A.** Running the pass while the `thetvdb` alias is broken re-fetches 87k shows and writes null anyway.
2. **Specials before pass C.** Pass C generates guest-cast rows keyed to episodes; 5.7% of them point at episodes we don't have. With the FK enforced those rows are dropped, and recovering them means re-running some fraction of a 75-hour pass with no record of which persons were affected. Merging specials into pass A costs 13.5h up front and makes the 75h pass correct on first run.
3. **Table definitions before either pass.** There is no cheap second attempt.

**Two milestones, both in v0.2.x.** Splitting them rather than shipping together means pass C's tables get exercised by real reads — show pages joining to `person` — before three days of unrepeatable budget goes into filling them, and it puts something user-visible in front of people after ~3 days instead of ~7.

## Testing strategy

**Unit (no DB)**

- `api_payloads` parsing of a raw cast entry, crew entry, person object and guest-cast credit. Include a person with `birthday: ""` and `deathday: ""` asserting they parse to `None` — the `OptionalDate` trap.
- A regression test in the shape of NEU-922's: assert a **raw** upstream payload populates every field we care about. That class of bug (wrong alias → silently null column) is invisible without one.
- Parsing of `_links` hrefs into ids on person-side credits.
- `sort_order` assignment preserves upstream array order.

**Integration (DB-backed)**

- `upsert_show_credits` — insert, then re-upsert with a member removed, asserting the removed row is gone and ordering survives.
- `upsert_person_credits` — same at person grain.
- `crew_role` resolve-or-insert is idempotent across runs.
- A show with >1,000 cast rows exercises the batch path (fixture, not live Simpsons).
- Pass A and pass C orchestrators: resumability (watermark set only on success), per-show/per-person failure counting, consecutive-failure abort.
- Route tests via `AsyncClient(ASGITransport(app=app))` — never `TestClient` — for every new endpoint, including empty-state responses for a show with no cast and an episode with no guest cast.

**Frontend** — vitest + MSW handlers for the new endpoints, covering populated and empty states for both.

## Migration / rollout

1. Migrations: create the six tables, add `show.credits_synced_at` and `person.credits_synced_at`, widen `ingest_run.kind`, rewrite its CHECK, add the person trigram index. **Migrations live in `tvbf-backend/migrations/`, not `alembic/`** (CLAUDE.md is stale); current head is `c2e451aa1ec6`. Note also that the test suite builds its schema from `Base.metadata.create_all`, not from migrations — model correctness is what the suite proves, so migration correctness needs `task migrate` against a real database.
2. Land NEU-922 and the NEU-933 specials fetch fix.
3. Run pass A in prod. ~27h. Poll via the status route.
4. Verify: re-measure `externals_tvdb` coverage and report on NEU-59 — it feeds the TMDB mapping-strategy decision. Confirm specials are present and `show_cast` is populated. Precedent: NEU-66 ("Verify AKA backfill completion in prod").
5. Ship milestone 1 frontend.
6. Land the person subsystem. Run pass C. ~75h.
7. Verify and ship milestone 2.

## Risks

| Risk | Mitigation |
|---|---|
| 102h of unrepeatable rate-limited budget | Freeze table definitions before any pass runs; both orchestrators resumable by watermark |
| Pass A runs before NEU-922 → 27h writing nulls | Hard blocker wired in Linear |
| Pass C runs before specials → 5.7% of guest cast silently dropped | Specials merged into pass A (decision recorded above) |
| Bind-parameter ceiling on large casts | Per-table batch constants, tested with a >1,000-row fixture |
| A person deleted upstream leaves orphaned credit rows | Accepted — persons and characters are never GC'd, same as `genre` and `network`. Orphans are harmless and GC would be a footgun mid-pass |
| Two ingest axes = two ways to be stale | Admin status routes per axis; "mirror is healthy" now needs a per-axis answer |

## Open questions resolved during grilling

- **What rides pass A?** Cast, crew, `externals_tvdb`, `rating.average`, and specials. The first four are free; specials cost +13.5h and are carried anyway because they gate pass C's integrity.
- **One `credit` table or several?** Several, mirroring upstream. A cast credit is *person-as-character*; a crew credit is *person-in-function*. Unifying them makes the common read (a show's cast, where crew outnumbers cast ~2:1) pay for the rare one.
- **Snapshot or tombstone?** Snapshot. Upstream never prunes, so it loses nothing.
- **Is there a season cast?** No. 404. Show and episode are the only grains.
- **Mirror or fetch on demand?** Mirror. See [ADR-0002](../../adr/0002-no-upstream-api-in-request-path.md).
- **How is guest cast obtained?** From the person side, one request per person. The 3.42M-episode walk (~22 days) never happens.
- **One-shot or peer subsystem?** Peer subsystem with a daily delta. A 75-hour investment that starts rotting on day one is a bad trade, and the delta costs ~9 minutes/day.
- **Does cast/crew feed search?** As a separate entity only.
- **Crew role: lookup or free text?** Lookup, modeled on `Genre`.
- **Cast ordering?** Explicit `sort_order` from the upstream array index. Billing order is only available from the show side.

## Open items

- **The ratings backfill has most likely already run in prod.** NEU-161 is Done and the owner believes the run completed; the local mirror's `ratings_synced_at` of 0/87,395 reflects a stale dev database, not prod. So `rating.average` is expected to be a **no-op rider** on pass A rather than a real one. Pass A should stamp `ratings_synced_at` regardless — it costs nothing and is correct either way. Worth one confirming query against prod before the pass runs; it changes no decision here.

## References

- [ADR-0001: `tvmaze` has two ingest axes](../../adr/0001-tvmaze-second-ingest-axis.md)
- [ADR-0002: No upstream API call in a live request path](../../adr/0002-no-upstream-api-in-request-path.md)
- [CONTEXT.md](../../../CONTEXT.md) — glossary
- Prior art for the orchestrator shape: `src/tvbf/tvmaze/akas_backfill.py`, `src/tvbf/tvmaze/ratings_backfill.py`
- Prior art for delete-then-insert: `upsert_akas`, `src/tvbf/tvmaze/upsert.py:218`
- Prior art for a local-id lookup on an upstream bare string: `Genre`, `src/tvbf/tvmaze/models.py:49`
- Prior art for folded trigram search: `2026-06-29-neu-433-search-normalization-design.md`
