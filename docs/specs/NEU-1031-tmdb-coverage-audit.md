# TMDB coverage audit: what we capture, what we skip, and why

**Ticket:** NEU-1031 · **Milestone:** 3 (Catalog ingest) · **Status:** decided
**Blocks:** NEU-1032 (`catalog` table definitions), NEU-1037 (frontend status filter vocabulary)
**Related:** [ADR-0007](../adr/0007-tmdb-replaces-tvmaze-as-the-catalog-source.md), [ADR-0008](../adr/0008-user-data-references-internal-ids.md)
**Parent:** the TMDB-migration project spec, which lives in the umbrella `docs/specs/` (not
version-controlled). This document is in the repo because NEU-1037 and NEU-1032 must cite it.

This is the column-by-column contract the rest of milestone 3 builds against. Nothing
else in the milestone starts until it lands.

**Every field list here was measured against the live API on 2026-08-09**, not recalled
from documentation, and every count against our own mirror. Two claims are *not* measured
and are marked where they appear: the three TMDB status values our 7-show sample did not
happen to contain, and TMDB's documented 20-entry append cap (which NEU-1028 did measure).
Where a claim rests on a sample, the sample size is named. Two of the ticket's own
inventories turned out to be incomplete; see [§8](#8-corrections-to-the-tickets-inventories).

## How to read the classifications

Every field is exactly one of:

| | Meaning |
| -- | -- |
| **Modeled** | A column (or table) lands in `catalog` in NEU-1032. |
| **Skipped** | Deliberately not stored, with a reason. Not "we didn't think about it." |
| **Deferred** | Not stored now, **with the re-fetch cost stated** so the cost is visible at the moment of deferral rather than discovered later. |

**Nothing here ended up deferred, and that is the finding.** Stating the re-fetch cost
honestly is what killed the category. The cost of adding a field *later* is never the zero
it looks like — the field rides a call we are already making *today*, but adding it in six
months means **a fresh pass over all 89,025 shows: ~1.5 hours and ~110,000 requests**, plus
a migration and a backfill job to own. That asymmetry is the entire argument for capturing
everything now, and once written down it leaves no field worth deferring. Every entry below
is therefore **modeled** or **skipped on merit** — skipped because it is not a catalog fact,
never because it was too expensive.

Skipped entries still state their reversal cost, for the same reason.

---

## 1. The constraint that shapes everything: the append budget

`append_to_response` caps at **20 entries**, measured; 21 is a hard HTTP 400 (NEU-1028,
`scripts/probe_tmdb_append_limit.py`). Namespaces and `season/N` entries draw on the same
20, so **every namespace costs a season slot**, and any season that does not fit needs its
own `get_tv_season` call.

Measured against our mirror (89,018 shows carrying seasons, 188,134 seasons, max 94
seasons on one show):

| Namespaces | Season slots | Overflow season calls | Total requests | Wall clock @ 20 req/s |
| --: | --: | --: | --: | --: |
| 3 (today's `DEFAULT_APPEND`) | 17 | 8,966 | 97,984 | 1.36 h |
| 5 | 15 | 10,924 | 99,942 | 1.39 h |
| **11 (decided)** | **9** | **21,584** | **110,602** | **1.54 h** |
| 15 (everything) | 5 | 38,481 | 127,499 | 1.77 h |

**Decision — take 11 namespaces.** The marginal cost of going from the current 3 to 11 is
**+12,618 requests ≈ 11 minutes** on a pass that runs an hour and a half either way. Against
that, each namespace omitted is a field that becomes a multi-hour backfill later — which is
exactly how this repo accumulated four of them under TV Maze. Eleven is not a compromise
between coverage and cost; it is *everything that is a catalog fact*, and the four dropped
namespaces are dropped on merit, not budget.

```
DEFAULT_APPEND = (
    "aggregate_credits", "alternative_titles", "content_ratings", "episode_groups",
    "external_ids", "images", "keywords", "screened_theatrically", "translations",
    "videos", "watch/providers",
)
```

The row counts cover the 89,018 shows that carry seasons; 7 shows have none and cost one
series call each, which the totals omit.

`plan_append()` already does this arithmetic and must stay the only place it lives
(NEU-1028) — a caller slicing by hand would keep computing the old split after the list
changed, and TMDB answers a short list with a clean 200.

---

## 2. The four decisions

### D1 — Status vocabulary: adopt TMDB's verbatim, and derive `is_ended`

**Measured** (7 shows): TMDB returned only `Returning Series` and `Ended`. `Planned`,
`In Production` and `Canceled` are **documented, not observed** — a 7-show sample cannot
establish a vocabulary, and NEU-1032 should confirm the full set against a larger sweep
before the frontend hard-codes five options. `in_production` is a genuinely separate
boolean, and that *is* measured — The Simpsons is `Returning Series` + `in_production:
true`; Breaking Bad is `Ended` + `false`.

Today's vocabulary and its distribution in our mirror:

| Ours | Rows | TMDB equivalent |
| -- | --: | -- |
| `Ended` | 67,745 | `Ended` **and** `Canceled` |
| `Running` | 14,335 | `Returning Series` |
| `To Be Determined` | 5,037 | **none** |
| `In Development` | 1,908 | `Planned`, `In Production` |

**Decision: store TMDB's string verbatim; do not translate.** A translation layer would be
maintained forever, and TMDB's vocabulary is strictly more informative — `Canceled` is a
distinction we currently cannot express at all.

**Decision: add a derived boolean `is_ended = status IN ('Ended', 'Canceled')`**, and route
every existing `status == "Ended"` comparison through it. This is the one piece of status
that carries behavior, and a straight port would silently reclassify every canceled show as
still-running, breaking "finished" for exactly the shows most likely to be finished.

**`To Be Determined` disappears.** It has no TMDB counterpart. Where those 5,037 shows land
is **not predictable from our data** — TMDB assigns each its own status independently, so the
only way to know is to read it after ingest. NEU-1032 should report the resulting distribution
rather than assume one. The frontend filter key is removed (NEU-1037), along with the comment
in `filterTypes.ts` explaining why TBD is excluded from "upcoming".

**Breaking change, day one:** status is rendered raw at `ShowList.tsx:40` and
`ShowDetailPage.tsx:103`, so users see the literal strings "Returning Series" and "Canceled"
the moment the catalog switches. If those labels are unwanted, the display map is frontend
work in NEU-1037 — the stored value stays TMDB's.

Post-migration filter set: **All · Returning Series · Ended · Canceled · In Production · Planned.**

### D2 — Specials: adopt season 0, and stop counting them

The two sources disagree structurally, and by far more than "a different convention":

| | TV Maze (today) | TMDB |
| -- | -- | -- |
| A special is | `number IS NULL`, season = *its real season* | `season_number = 0`, with a real `episode_number` |
| Count in our mirror | **27,498** null-number episodes | 67 episodes in season 0 |
| Overlap | 40 episodes are both | |

So 27,458 of our specials carry a real season number and a null episode number — they cannot
become season-0 rows without losing which season they belonged to, and they have no
`(season_number, episode_number)` counterpart to map to.

**Decision: adopt TMDB's representation — season 0, real episode numbers — and redefine
"Special" in `CONTEXT.md` accordingly.** The null-number definition is retired. **That edit is
NEU-1032's to make, not this ticket's** — `CONTEXT.md` is deliberately untouched here, because
the glossary should change when the code does. Both of its sentences go stale: the definition
itself, and "Excluded from the episode embed, so it requires its own fetch", which is a TV Maze
artifact — TMDB returns specials in the `season/0` payload like any other season.

**Decision: exclude season 0 from completion math, aired counts and Watch Next.** This is
TMDB's own semantics, measured: `number_of_episodes` and `number_of_seasons` **exclude
season 0** (The Simpsons 802 episodes / 38 seasons, with 83 further episodes and a 39th
entry in `seasons[]`; Breaking Bad 62 / 5, with 9 more). Following TMDB here also fixes a
latent inconsistency: `episode_repo` selects aired episodes on `airdate IS NOT NULL AND
airdate <= today` with **no exclusion for specials**, so **today a past-dated special already
counts toward "caught up"**, and a user can be held short of finished by a DVD extra.

This is a deliberate behavior change, not a port. It is the reason NEU-1032 must carry a
`season_number = 0` predicate into the aired-count queries rather than copying them across.

### D3 — Languages: store all three, filter on `original_language`

TV Maze had one `language`; TMDB has three, and they genuinely differ — Breaking Bad is
`original_language: "en"`, `languages: ["en","de","es"]`, `spoken_languages: [English,
German, Spanish]`.

**Decision: model all three.** `original_language` as a scalar column, `languages[]` and
`spoken_languages[]` as their own tables. Collapsing to one column would be lossy, and the
cost of not collapsing is two join tables on data that arrives free.

**Decision: the browse `language` filter reads `original_language`.** It is the closest
equivalent to TV Maze's single value and the only one with one value per show, which the
filter's exact-match semantics require. Note the *values change shape*: TV Maze stored
`"English"`, TMDB stores `"en"` — 84 distinct values today, all of which become ISO 639-1
codes. Three consequences, all frontend: the filter picker needs a code→label map; any bookmarked
`?language=English` returns nothing; and every **persisted `localStorage` language filter
becomes a silent dead end**. Note this is worse than the sort keys' behaviour —
`usePersistedString` does *no* validation (unlike `usePersistedSort`, which checks against an
allowed list), so a stored `"English"` is restored verbatim and sent to the API, and the user
sees an empty list with a filter that looks valid. NEU-1037 should bump the storage key or
validate the value, not rely on it aging out.

### D4 — Runtime: derive it, because TMDB's own field is usually empty

**Measured: `episode_run_time` was `[]` for 6 of 7 sampled shows** — Breaking Bad, The
Simpsons, Arcane, Rings of Power, The Flash and Dan Da Dan all return an empty array; only
Frieren returned `[25]`. Treating it as the show's runtime would blank the field for most of
the catalog.

**Decision: store `episode_run_time[]` verbatim *and* compute a scalar `runtime` as the
median of the show's episode runtimes at ingest.** Episode-level `runtime` is populated for
**94.9% of episodes** (3,349,375 of 3,530,808, measured on our mirror), so the derived value
is both far more available than TMDB's series-level field and more accurate.
`ShowDetailPage.tsx:104` renders `show.runtime` and keeps working unchanged.

Median rather than mean: a single feature-length finale should not drag a 22-minute
sitcom's runtime upward.

**Ingest constraint this imposes on NEU-1032:** the show row's `runtime` cannot be finalised
until every season's episodes are in hand, including any `get_tv_season` overflow, and it must
be recomputed whenever the daily delta adds episodes. Either write the show first and update
`runtime` at the end of the show's ingest, or accept a stale value for one cycle. This is a
real ordering dependency, not an implementation detail.

---

## 3. Inventory — series level

Measured top-level keys, Breaking Bad, 2026-08-09.

| Field | Class | Reason |
| -- | -- | -- |
| `adult` | Modeled | Content gate; free. |
| `backdrop_path` | Modeled | Public Profiles & Sharing needs it; skipping is a backfill. |
| `created_by[]` | Modeled | Show creators — a credit type TV Maze never had. |
| `episode_run_time[]` | Modeled | Stored verbatim; see [D4](#d4--runtime-derive-it-because-tmdbs-own-field-is-usually-empty). |
| `first_air_date` | Modeled | = `premiered`. Drives sort + the mapping tier-3 year check. |
| `genres[]` | Modeled | = `genre`; browse filter (28 genres today). |
| `homepage` | Modeled | = `official_site`. |
| `id` | Modeled | As `tmdb_id`, **not** the primary key. ADR-0008 makes the PK an internal surrogate; the *project spec* then seeds those surrogates from the existing TV Maze ids, so a migrated show's PK is its old `tvmaze.show.id`. Two separate decisions. |
| `in_production` | Modeled | Separate boolean carrying information `status` does not (D1). |
| `languages[]` | Modeled | D3. |
| `last_air_date` | Modeled | = `ended`. |
| `last_episode_to_air{}` | Modeled | A denormalised pointer, refreshed by the daily delta. Derivable from stored episodes, but see the note below on why that is not a reason to drop it. |
| `name` | Modeled | |
| `next_episode_to_air{}` | Modeled | Same. The ticket lists it as a Push Notifications / Upcoming backfill risk, and TMDB's own answer settles unaired-episode edge cases that a local query has to guess at. |
| `networks[]` | Modeled | Absorbs both `network` and `web_channel` (1,479 + 668 rows today). |
| `number_of_episodes` | Modeled | Stats / Year in Review. **Excludes season 0** (measured). |
| `number_of_seasons` | Modeled | Same; excludes season 0. |
| `origin_country[]` | Modeled | |
| `original_language` | Modeled | Browse filter (D3). |
| `original_name` | Modeled | TVBF: Localization *is* this field plus `translations`. |
| `overview` | Modeled | = `summary`. |
| `popularity` | Modeled | Replaces `weight` — which nothing reads (§6). |
| `poster_path` | Modeled | = `image_medium` / `image_original`. |
| `production_companies[]` | Modeled | Free on the same call. |
| `production_countries[]` | Modeled | Free on the same call. |
| `seasons[]` | Modeled | Drives `plan_append`; includes the season-0 entry. |
| `softcore` | Skipped | **Not in the ticket's inventory** — present in the live response, undocumented, no product use. Revisit only if TMDB documents it. |
| `spoken_languages[]` | Modeled | D3. |
| `status` | Modeled | D1. |
| `tagline` | Modeled | |
| `type` | Modeled | `Scripted` / `Reality` / … ; = TV Maze `type`. |
| `vote_average` | Modeled | = `rating_average`. |
| `vote_count` | Modeled | New; lets us weight ratings rather than trust a 10.0 from three voters. |

Both `*_to_air` fields are derivable from the episode rows we store, and `/me/upcoming`
derives the equivalent today. They are modeled anyway because a derivable field still costs a
full re-ingest to add later, while a nullable FK column costs nothing now — and because they
are TMDB's own answer, which is authoritative where ours is inference. Treat them as a cache:
the daily delta refreshes them, and nothing should read them where a live query is available.

## 4. Inventory — season and episode level

**Season** (measured keys: `_id`, `air_date`, `name`, `networks`, `overview`, `poster_path`,
`season_number`, `vote_average`, `episodes`)

| Field | Class | Reason |
| -- | -- | -- |
| `air_date` | Modeled | = `premiere_date`. |
| `episode_count` | Modeled | From `seasons[]` on the series payload. |
| `id` | Modeled | As `tmdb_id`; PK stays the preserved TV Maze season id. |
| `name`, `overview`, `poster_path`, `season_number` | Modeled | Direct equivalents. |
| `vote_average` | Modeled | New at season grain. |
| `networks` | Modeled | **Not in the ticket's inventory**; measured on the season payload. Mirrors the series field. |
| `episodes[]` | Modeled | The whole reason `season/N` is appended. |
| `_id` | Skipped | TMDB-internal, per the ticket. |

**Episode** (measured keys exactly match the ticket's list)

| Field | Class | Reason |
| -- | -- | -- |
| `air_date` | Modeled | = `airdate`. **No `airtime` equivalent** — see §6. |
| `episode_number`, `season_number`, `show_id`, `id` | Modeled | Identity and ordering. |
| `episode_type` | Modeled | `premiere` / `mid_season` / `finale` / `standard` — measured. Finale detection with no heuristics; skipping it is a Push Notifications backfill. |
| `name`, `overview`, `runtime`, `still_path` | Modeled | Direct equivalents; `runtime` also feeds D4. |
| `production_code` | Modeled | Free; occasionally the only stable id for a special. |
| `vote_average`, `vote_count` | Modeled | New at episode grain. |
| `crew[]`, `guest_stars[]` | Modeled | Replaces the 29-hour `credits_backfill` with data that rides the season call. |

## 5. Inventory — person (inside credits) and namespaces

**Person**, measured on `aggregate_credits` and episode `guest_stars` / `crew`:

| Field | Class | Reason |
| -- | -- | -- |
| `id`, `name`, `original_name` | Modeled | Identity. |
| `gender`, `known_for_department`, `popularity` | Modeled | Person pages; free. |
| `profile_path` | Modeled | Person pages. |
| `adult` | Modeled | Consistency with the series flag. |
| `credit_id` | Modeled | The stable id for a specific role, which character-as-free-text otherwise lacks. |
| `character` + `order` (cast/guest) | Modeled | `character` interned per show (project spec); `order` replaces the billing-order proxy. |
| `department` + `job` (crew) | Modeled | = `crew_role`. |
| `roles[]` / `jobs[]` + `total_episode_count` | Modeled | **Not in the ticket's inventory.** `aggregate_credits` nests these; `episode_count` per role is strictly better than today's billing-order proxy. |

**Namespaces** — 11 taken, 4 dropped:

| Namespace | Class | Reason |
| -- | -- | -- |
| `aggregate_credits` | Modeled | Cast/crew with per-role episode counts. Supersedes `credits`. |
| `alternative_titles` | Modeled | = `show_aka`; AKA search survives intact. |
| `content_ratings` | Modeled | Age ratings; no TV Maze equivalent exists. Measured shape: `results[]` of `{iso_3166_1, rating, descriptors[]}`. |
| `episode_groups` | Modeled | Absolute and DVD orderings — anime and long-running shows. |
| `external_ids` | Modeled | The migration's mapping tiers depend on `tvdb_id` and `imdb_id`. |
| `images` | Modeled | `backdrops` / `logos` / `posters`. |
| `keywords` | Modeled | Discovery / Personalized Recommendations. |
| `screened_theatrically` | Modeled | Rare but one slot (~2,800 requests ≈ 2 min). |
| `translations` | Modeled | TVBF: Localization. Measured: 48 for The Simpsons; `data` = `{homepage, name, overview, tagline}`. |
| `videos` | Modeled | Trailers; free. |
| `watch/providers` | Modeled | The capability that justifies TMDB beyond raw coverage. |
| `credits` | **Skipped** | Strictly weaker than `aggregate_credits`, which carries the same cast plus `order`, `roles[]` and episode counts. Storing both duplicates the same people. |
| `recommendations` | **Skipped** | TMDB-computed, paginated, and volatile — not a catalog fact. It would be stale before the ingest finished and we would never refresh it. |
| `similar` | **Skipped** | Same reasoning. |
| `reviews` | **Skipped** | User-generated content belonging to another product, paginated, with moderation exposure we do not want to inherit. |
| `season/N` | Modeled | Not a namespace but the same budget line, and the reason the budget binds at all: it carries a season's full episode list. Nine ride the series call; the rest overflow to `get_tv_season`. |

Reversing any of the four dropped namespaces costs **one season slot plus a full re-ingest** —
the slot pushes 2,558 shows (2.9%, those with more than 9 seasons) into one extra season call,
about 3,038 requests, and the pass to populate the new column is the same ~1.5 hours as any
other. Cheap in slots, not free in time. None was dropped for cost.

---

## 6. Known losses — confirmed against the code

| TV Maze | TMDB | Verdict |
| -- | -- | -- |
| `schedule` (day/time) | none | **No loss at all.** `schedule` was never ingested — it is absent from `tvmaze/models.py` and read nowhere in either repo. |
| `weight` | `popularity` | **No loss.** `weight` is read nowhere in `src/` or the frontend; nothing sorts on it. Different scale is therefore irrelevant. |
| `network` / `web_channel` split | merged `networks` | **Nearly free.** The frontend renders `show.network?.name` only (`ShowList.tsx:40`, `ShowDetailPage.tsx:102`) and never `web_channel`. Backend read sites: `browse.py`, `browse_queries.py`, `schemas.py`, `my_shows_service.py`, `upsert.py`, `models.py`. Browse filters reshape from two lists to one. |
| `show_aka` | `alternative_titles` | Direct equivalent; the GIN trigram index and AKA-aware search survive. |
| Global character ids | free text per credit | Interned per show. **1,509,298 characters** in our mirror today (the project spec's 1,508,888 is stale — the catalog has grown); of those, 2,621 are multi-person, all preserved by per-show interning, and exactly one spans more than one show. |
| Episode `airtime` | none | **New loss, not in the ticket's table.** TMDB carries `air_date` (a date) with no time component. `tvmaze.episode.airtime` is stored and served (`models.py:162`, `schemas.py:88`, `upsert.py:196`) and appears in the frontend DTO at `types.ts:68` — but **no component renders it**, so the loss is contained to the API contract. `OptionalTime` exists solely to parse this one field and retires with it. |

## 7. Read sites of changed fields

Every site that must change, by decision. Line numbers verified against the tree at
`849505c`.

**D1 status** — backend: `browse_queries.py:357-358` (exact-match filter),
`my_shows_service.py:673` (`is_ended`), `schemas.py:277,319` (pass-through to the API),
`upsert.py:133`, `models.py:74`. Frontend: `filterTypes.ts:31-38` (the filter list),
`:53` (finished vs caught_up), `:65-73` (`matchesStatus`), `SearchOverlay.tsx:36-39`
(labels), `MyShowCard.tsx:28` (`=== "Ended"`), and the two raw renders,
`ShowList.tsx:40` and `ShowDetailPage.tsx:103`. Tests carrying status literals:
`test_browse.py`, `test_browse_queries.py`, `test_my_shows_service.py`,
`test_watched_service.py`, `test_me_export.py`, `test_people_routes.py`,
`fixtures/browse/seed.py`.
*Not a read site:* `admin.py:207` serialises `IngestRun.status`, which is unrelated.

**D2 specials** — backend: `episode_repo.py:27-50` (`aired_count_per_season` and
`list_aired_episode_ids_for_show`), `:79-80,97-98,120-121` (further aired filters, all
`airdate IS NOT NULL AND airdate <= today`), `:149` (the *future*-dated filter, which needs
the same season-0 exclusion for Upcoming), and the orderings at `:125`
(`season, number`), `:153` (`airdate, season, number`) and `:177` (`season, number`).
Frontend renders `S{season}E{number}` at `NextEpisodeCard.tsx:47`, `WatchNextList.tsx:156`,
`UpcomingList.tsx:151` and `FeedItemRow.tsx:24-25`.
**Note the ordering flip:** a null episode number sorts *last* under Postgres's default, so
today's specials trail their season; season-0 specials sort *first*, ahead of the premiere,
unless excluded.

**D3 languages** — backend: `browse.py:74,89` (query param → filter),
`browse_queries.py`, `schemas.py:24,108,278,320`, `models.py:73` (`Show.language`).
Frontend: `client.ts:71` (filter param), `ShowList.tsx:40` (rendered inline),
`types.ts:85,233`, `me.ts:166`.
*Not a show-level site:* `models.py:118` is `ShowAka.language`, which maps to
`alternative_titles[].iso_3166_1` and is unaffected by D3.

**D4 runtime** — backend: `models.py:75` (show) and `:163` (episode), `schemas.py:89,123,329`,
`upsert.py:134,197`. Frontend, show runtime: `ShowDetailPage.tsx:104`. Frontend, episode
runtime (**unaffected** — episode `runtime` ports directly): `WatchNextList.tsx:163-167`,
`UpcomingList.tsx:158-162`, `EpisodesPage.tsx:228`, `EpisodePage.tsx:144,147-148`.

**`web_channel` merge** — backend: `models.py`, `schemas.py`, `browse.py`,
`browse_queries.py`, `my_shows_service.py`, `upsert.py`, and the ingest entry point
`api_payloads.py:96,238` (spelled `webChannel`, TV Maze's camelCase). Frontend: rendered
nowhere; it appears only in test fixtures.

## 8. Corrections to the ticket's inventories

Measured against the live API, three additions and one removal:

1. **`softcore`** — a series-level field the inventory does not list. Classified *skipped*.
2. **Season-level `networks`** — present on the season payload, not in the season inventory.
   Classified *modeled*.
3. **`aggregate_credits` nests `roles[]` / `jobs[]` and `total_episode_count`** — the person
   inventory lists `character`/`order`/`department`/`job` as flat fields, which is the shape
   of `credits` and episode `guest_stars`, not of `aggregate_credits`.
4. **`external_ids` is richer than assumed** — measured: `imdb_id`, `tvdb_id`, `tvrage_id`,
   `wikidata_id`, `freebase_id`, `freebase_mid`, `facebook_id`, `instagram_id`, `twitter_id`.
   All modeled; `tvdb_id` and `imdb_id` carry the migration's mapping tiers.

Also worth recording: **the episode-level inventory was exactly right** — the measured episode
keys match the ticket's list field for field.

## 9. Acceptance criteria

- [x] Every field in all five inventories classified modeled / skipped / deferred with a
      one-line reason — §3, §4, §5.
- [x] Status vocabulary, specials representation, language handling and runtime **decided** —
      §2 (D1–D4).
- [x] Every read site of a changed field listed, backend and frontend — §7.
- [x] Deferred items state what would need re-fetching — **vacuously satisfied: nothing is
      deferred.** Writing the cost down honestly (a full ~1.5 h re-ingest, not "zero") is what
      collapsed the category; see [How to read the classifications](#how-to-read-the-classifications).
      Skipped items state their reversal cost anyway — §5.
