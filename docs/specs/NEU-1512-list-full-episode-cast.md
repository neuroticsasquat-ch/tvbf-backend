# NEU-1512 — List full episode cast: season regulars, and the credits pages built on them

**Ticket:** [NEU-1512](https://linear.app/neuroticsasquatch/issue/NEU-1512/list-full-episode-cast)
**Repos:** `tvbf-backend` (§3 ingest, §4 contract) and `tvbf-frontend` (§5 pages) — branch `tom/neu-1512-list-full-episode-cast` from `main` in each. Three PRs in order: backend ingest, backend routes, SPA (§6).
**Project:** tvbf: Maintenance
**Absorbs:** NEU-1514 (per-season regulars), which this spec makes phase one rather than a follow-up
**Precedents this consumes:** `src/tvbf/tmdb/credits_backfill.py` + `jobs/credits_backfill.py` (the resumable per-show backfill this copies), `src/tvbf/tmdb/upsert.py` `_write_episode_credits` / `_refresh_scope` / `_write_season_networks` (the None-vs-`[]` rule and the season-scoped replace), `src/tvbf/tmdb/client.py` `plan_append` (the 20-entry append budget), `scripts/probe_tmdb_episode_credits_append.py` (the probe this copies), ADR-0007 (per-show character interning, one `crew_role` vocabulary), `docs/specs/NEU-1031-tmdb-coverage-audit.md` (which skipped `credits` and, with it, season credits), `tvbf-frontend/docs/specs/NEU-1209`, `NEU-1210`, `NEU-1211`, `NEU-1007` (the tab and grouping patterns this reshapes)
**Glossary:** `CONTEXT.md` — **Regular credit**, **Series crew credit**, **Season cast** (added by this design session); **Cast credit**, **Crew credit**, **Guest credit**, **Billing order**, **Filmography**, **Credit group** amended
**Status:** approved for implementation. §3.1's probe ran 2026-09-30 and chose **Route A** (§3.2): `season/N/credits` rides the series append, identical to the standalone response on all 8 probed seasons; the standalone season takes `append_to_response=credits`; 0 of 32 regulars lacked `id`/`name` (too few to go strict — §3.3 stays lenient). PR 1 (ingest) implements §3.

This spec lives in `tvbf-backend/docs/specs/` because it is a cross-repo
contract: §4 changes the credit routes the SPA reads and §5 is written against
§4. `tvbf-frontend/docs/specs/NEU-1512-list-full-episode-cast.md` is a pointer.

---

## 1. The problem, measured

The ticket reports three symptoms — an episode page lists guests but no
regulars; the show page's Cast tab lists everyone who ever appeared, regular or
guest; a person page lists every guest appearance under Cast as well as under
Guest. They have one cause, and it is upstream's data model rather than ours.

**TMDB records regular cast per season, guest stars and crew per episode, and
only guest stars and crew ride the payload we ingest.** TMDB's contributor
guidance is explicit: the regular cast section of a *season* is for people with
series-regular status on that season's on-screen credits; anyone else is added
as a guest star on the *episodes* they are in; a regular is credited on every
episode of the season whether or not they appear; and every season is
independent, so someone can guest in seasons one and two and be a regular from
season three. That fact is served by `GET /tv/{id}/season/{n}/credits`
(`cast[]`, one entry per regular, with `character`, `credit_id` and `order`).
The per-episode credits call repeats the same list as its `cast` on every
episode of the season. The `episodes[]` block inside a season payload — what
the ingest reads — carries `guest_stars` and `crew` and no `cast` key; both
migration probes confirmed the key set.

The coverage audit (NEU-1031) skipped `credits` as "strictly weaker than
`aggregate_credits`", which is true at show grain, and season credits went
unfetched with it. So today a regular exists in the catalog exactly once: as a
`show_cast` row from `aggregate_credits`, with an episode count and **no season
or episode attached**. Measured on the workspace database (Postgres 18, the
full catalog: 231k shows, 397k seasons, 6.6M episodes) on 2026-09-30:

| Measurement | Value |
| -- | -- |
| `show_cast` rows | 3,128,580 |
| … with **zero** rows in `episode_guest_cast` (regulars: nothing ties them to a season or episode) | 1,026,679 (33%) |
| … whose aggregate `episode_count` equals their guest credits (guest-only) | 2,079,369 (66%) |
| … whose aggregate count exceeds their guest credits (regular in some season, guest in another) | 14,448 |
| Law & Order regulars with any per-episode row | 0 of 33 (of 11,550 cast rows) |
| `show_crew` jobs with no episode-crew rows (series-level roles) | 597,229 of 1,096,084 |
| Shows with exactly one season | 167,204 of 212,123 (79%) |
| Seasons in the catalog / of which the append cap already overflows | 397,058 / 52,307 |
| Shows the daily delta re-fetches | 1,300–2,300 a night, ~22 min |

Two consequences shape everything below. First, **no query over what we hold
can put a regular on a season or an episode**, so a season page or an episode
page built from episode credits alone shows guests and crew and no leads. The
33% has to be fetched, once, from the one endpoint that has it. Second, once
season regulars are in the catalog, **"regular" stops being something we
infer** and the regular/guest split at every grain is a set difference against
upstream's own list.

## 2. Decisions

### 2.1 Season regulars are ingested, and they are the definition of "regular"

A new table, `catalog.season_cast`, holds one row per (season, person,
character) from `season/{n}/credits.cast[]`. From it:

- A **regular credit** at show grain is a (person, character) with a
  `season_cast` row on any season of the show. Its episode count and billing
  order come from the matching `show_cast` row (`IS NOT DISTINCT FROM` on
  `character_id`, both interned per show), its seasons from `season_cast`.
- A **guest** at show grain is a `show_cast` row with no such match. The show
  page's Cast and Guest stars tabs partition `show_cast` exactly; nothing is
  lost.
- A season's **regular cast** is its `season_cast` rows in billing order. A
  season's **guest stars** are the `episode_guest_cast` rows over its episodes,
  grouped by (person, character) with a count, minus anyone who is a regular
  *of that season* (an entry in both is upstream inconsistency; the season's
  own regular list wins at season grain).
- An episode's cast is **its guest credits, unchanged**. TMDB does not know
  whether a regular is in a given episode — it credits them on the whole
  season — so listing regulars on every episode would repeat one unverifiable
  block 22 times. The episode page links to the season's regular cast instead.

The count-based derivation an earlier draft of this spec proposed
(`aggregate count > guest credits ⇒ regular`) is **not** used. It reproduces
upstream on 99.7% of rows but cannot place anyone in a season, which is what
the season page and the person page need; and once season credits exist it is
redundant.

### 2.2 Crew keeps the derivation, because crew has no season-level source worth fetching

Season credits also carry `crew[]`, a per-season list of producers and the
like. It is **not ingested**: series-level crew is already in
`aggregate_credits`, per-episode crew is already in `episode_crew`, and a third
grain would add a table for a list no page asks for. Instead a **series crew
credit** is a `show_crew` job whose aggregate `episode_count` exceeds the
person's `episode_crew` rows in that role on that show:

```
coalesce(show_crew.episode_count, 0)
  > count(episode_crew rows for the same (show, person, role))
```

A director of forty episodes holds forty episode crew credits and no series
crew credit; the show-level row is a sum. Measured: 597k jobs have no episode
rows at all (Executive Producer, Creator, Composer), 486k have an equal count
(directors, writers, editors), 13k are mixed. The split runs in 4 ms on the
largest show and needs no index. Season crew is the season's `episode_crew`
rows grouped by (person, role) with a count.

### 2.3 What each page shows

- **Show page.** Cast = regulars, sorted by episode count descending with the
  count after the name; Guest stars = the rest, same sort; Crew = series crew;
  Episode crew = the rest, grouped by role. Long lists page client-side.
- **Season page** (`/shows/:id/episodes?season=N`, which already exists). A
  tab strip — Episodes, Cast, Crew — where Cast is the regular cast in billing
  order followed by the season's guest stars by appearances, and Crew is the
  season's crew by role with counts.
- **Episode page.** Guest cast and crew as today, plus one line linking to the
  season's cast: "Regular cast for Season 3".
- **Person page.** Two tabs, Cast and Crew. One entry per show, newest
  credited date first, with the episode count after the show name, and an
  expandable list beneath: the seasons they were a regular in (each a link to
  that season's cast, with the season's episode count) and the episodes they
  guested in. No regular/guest labelling anywhere; the grain is visible only in
  whether a row is a season or an episode.

### 2.4 The API splits; the client presents

Every route returns what one panel renders, and the person route keeps its
four lists but stops sending guest-only credits as if they were regular. The
alternative — one aggregate list with flags — would make the SPA the place
where "who is a regular" is decided, and the show page fetch 11,550 rows to
render 33.

## 3. Backend: ingest (PR 1)

### 3.1 Probe first — `scripts/probe_tmdb_season_credits.py`

Copy `probe_tmdb_episode_credits_append.py`: the same five series plus Law &
Order (TMDB 549, the largest cast) and a two-season show, run inside the
container with `TMDB_READ_ACCESS_TOKEN` set, a handful of requests per series.
It answers, and the PR description records:

1. **Does `season/{n}/credits` ride the series append?** Request
   `GET /tv/{id}?append_to_response=season/1,season/1/credits` and report
   whether a `season/1/credits` key comes back with `cast[]`, or an error, or
   nothing.
2. **Does the standalone season request take `append_to_response=credits`?**
   `GET /tv/{id}/season/1?append_to_response=credits` — TMDB documents
   `append_to_response` on the season detail method, so this is expected to
   work, but it is what the overflow path relies on and it is measured.
3. **Key sets and gaps** on `cast[]`: every key seen; entries missing `id` or
   `name`; blank `character`; `order` presence. (Show grain has never been
   measured to omit a person; episode grain does, 0.4% of shows — NEU-1128.
   This decides whether the season payload class is strict or lenient. Lenient
   is assumed below; strict is a one-line tightening if measured clean.)
4. **Parity:** for each probed season, the regular list against that season's
   `episodes[].guest_stars` (overlap count) and against `aggregate_credits`
   (regulars with no aggregate row). Informational; it sizes the edge cases in
   §4.

The answer to (1) picks the fetch route; nothing else in this spec depends on
it.

### 3.2 Fetch route

**Route A — season credits ride the series append.** `plan_append` emits
`season/N` *and* `season/N/credits` for each season it places, so the 8 season
slots become 4; `speculative_seasons` becomes `(0..3)`; overflow seasons are
fetched standalone by `get_tv_season(series_id, n, append=("credits",))`
(new keyword, default `()`), so every season arrives with its credits in the
request that carries its episodes. `TMDBSeries._collect_appended_seasons`
today gathers every `season/…` key as a season detail; it must route
`season/N/credits` into the matching detail's `credits` field instead.
Catalog-wide overflow rises from 52k to 90k standalone season requests; the
full pass grows by ~40k requests (~35 min at 20 req/s), the delta by a few
hundred a night. Test pins that change: the 8-slot pin in `test_client.py`,
`SPECULATIVE_SEASONS` in `test_ingest_plan.py`, the 33-overflow assertion in
`test_a_forty_season_show_is_fetched_completely` (becomes 37 — the window
0..3 catches seasons 1–3 of 1..40), and
`test_ingest.py`'s `mock_series` helper, which must serve the compound key.

**Route B — it does not.** The series append is untouched. Season credits are
one request per season, `GET /tv/{id}/season/{n}/credits`, made by
`fetch_series_with_seasons` after the overflow seasons (a new client method
`get_tv_season_credits(series_id, n)`), sequentially, sharing the show's
failure semantics: a season whose credits request fails fails the show, as an
overflow season does today. Full pass +397k requests (~5.5 h at 20 req/s); the
delta +3,800 a night (~3 min). No append pins change.

Under either route the writer sees the same thing: a `TMDBSeasonDetail` whose
`credits` is present or absent.

### 3.3 Payload

```python
class TMDBSeasonRegular(TMDBEpisodeCreditPerson):
    """One entry of a season's `credits.cast[]` — a season regular."""
    character: OptionalStr = None
    credit_id: OptionalStr = None
    billing_order: int | None = Field(default=None, alias="order")

class TMDBSeasonCredits(_Payload):
    """`season/{n}/credits`. `crew` is present upstream and ignored (§2.2)."""
    cast: list[TMDBSeasonRegular] = Field(default_factory=list)

class TMDBSeasonDetail(_Payload):
    ...
    credits: TMDBSeasonCredits | None = None   # None ⇒ key absent ⇒ out of scope
```

Lenient person identity (the episode-grain class) unless §3.1 (3) measures the
grain clean, in which case use `TMDBCreditPerson`. `_Payload`'s
`extra="ignore"` drops `crew` and the payload's `id`.

### 3.4 Table and watermark — one migration

`catalog.season_cast`, modelled on `EpisodeGuestCast`:

| column | type | notes |
| -- | -- | -- |
| `id` | bigint identity | `_surrogate()` |
| `season_id` | FK `season.id` ON DELETE CASCADE, NOT NULL | |
| `person_id` | FK `person.id`, NOT NULL | |
| `character_id` | FK `character.id`, nullable | interned **per show** — the season's show — so it matches `show_cast` and `episode_guest_cast` |
| `credit_id` | text, nullable | upstream's credit id |
| `billing_order` | int, nullable | from `order`; the season's billing order (`CONTEXT.md`, *Billing order*) |

`UNIQUE (season_id, person_id, character_id)` **`NULLS NOT DISTINCT`**, named
`uq_season_cast_season_person_character` (same reasoning as
`uq_egc_episode_person_character`: nullable character, re-ingest must not
duplicate). `ix_season_cast_person_id` for the person page. No index on
`season_id` alone; it leads the unique index. Show-grain reads reach the table
through `season.show_id` (`ix_season_show_id_number`).

`catalog.show.season_credits_synced_at timestamptz NULL`, beside
`credits_synced_at`, no index, no backfill — the backfill's work-list
predicate is `IS NULL`, and `credits_synced_at` has run at that scale
unindexed.

Migration: `<rev>_add_season_cast.py`, `down_revision = "28e392fdeb47"` (or the
head at the time), literal `schema="catalog"`, names as above. No `ingest_run`
kind: the credits backfill has none either (§3.6).

### 3.5 Writer — `_write_season_credits`

In `upsert.py`, called from `upsert_series_payload` immediately after
`_write_season_networks` (season surrogates are in hand there, matched by
`season_number → tmdb_id → id` exactly as networks are), and from a public
seam `write_season_credits` for the backfill path — its own seam rather than
`write_series_credits`, so the credits backfill cannot fill `season_cast`
without stamping its watermark. Signature mirrors
`_write_episode_credits`:

1. **Scope** = seasons whose detail has `credits is not None`, deduplicated by
   season id. A detail with no `credits` key leaves the season's rows alone;
   `credits.cast == []` clears them; a non-empty `cast` whose entries were all
   skipped for lacking a person holds the season back rather than emptying it
   — `_refresh_scope`'s three cases, unchanged.
2. Filter entries with `_has_person(..., grain="season regular")`, logging
   the skip.
3. Upsert people with `_person_row`, intern characters **per show** with
   `_character_name` blanking `""` to `None`.
4. Deduplicate on `(season_id, person_id, character_id)`; keep the first
   entry's `credit_id` and `billing_order`.
5. Delete-then-insert per scoped season, batched — a season-scoped twin of
   `_replace_episode_rows`.

`mark_series_synced` stamps `season_credits_synced_at` alongside the other
three, so the full pass and the delta keep every show current without a second
mechanism. `mark_credits_synced` gains a sibling `mark_season_credits_synced`
for the backfill.

### 3.6 Backfill — `tmdb/season_credits_backfill.py` + `jobs/season_credits_backfill.py`

A copy of `credits_backfill.py` with the nouns changed, because that shape has
run once over the whole catalog and its properties are the ones wanted:

- **Work list:** `show.tmdb_id IS NOT NULL AND tmdb_synced_at IS NOT NULL AND
  season_credits_synced_at IS NULL`, keyset-paged by `show.id`, 200 a page.
- **Per show:** fetch what §3.2's route needs — under A (chosen),
  `fetch_series_with_seasons(..., namespaces=())`: the pass writes one table,
  so the freed namespace slots widen the season window to 0..9; under B, only the season credits requests for the show's mirrored
  seasons, no series request — then `_write_season_credits` for every season
  that came back, then stamp. One commit per show; a show leaves the work list
  only when its stamp commits, which is what makes the pass resumable.
- **Failure semantics:** a 404 counts as `gone`, is left unstamped and does
  not count toward the abort; ten consecutive other failures raise
  `SeasonCreditsBackfillAborted`; any success resets the counter. A season
  whose detail lacks `credits` entirely (Route A returned nothing for it) is
  counted as `seasons_without_credits` and the show is **not** stamped, on
  `MissingCreditsNamespace`'s reasoning: an unstamped show is retried, a
  stamped one is believed.
- **Result / report:** `shows_considered, shows_stamped, shows_failed,
  shows_gone, seasons_written, seasons_without_credits`; `report` prints
  `shows_remaining`, `season_cast` row count, and the number of shows that are
  stamped yet have `show_cast` rows and no `season_cast` rows (the "TMDB lists
  no regulars" population — informational, expected to be non-trivial for
  small shows).
- **CLI and Taskfile:** `backfill [--limit N]` and `report`, exit 1 on abort,
  `Taskfile.yml` targets `backfill:season-credits` and
  `backfill:season-credits:report` beside `backfill:credits`. Run in
  production by hand over ssh as `docs/migration/README.md` records for the
  credits pass; record the run's date and totals in that README.

**Budget:** under B the pass is ~397k requests at the configured 20 req/s,
~5.5 h; under A (chosen) it is one request per show, ~231k, plus overflow only
for the few percent of shows with a season past 9 — at the ~7.5 req/s the
sequential loop has measured on every catalog pass, ~8.5 h. Resumable, so it
can span evenings, and the delta keeps it current afterwards.

## 4. Backend: contract (PR 2 — ships after the backfill's `report` shows zero remaining)

All routes keep the router-level `get_current_user`, `Cache-Control: private,
max-age=300`, no pagination, and 404 on an unknown parent. Inner joins to
`character` stay (null-character rows are dropped as today). Queries in
`browse_queries.py`, routes in `routers/browse.py`, schemas in `schemas.py`.

### 4.1 Schemas

- `CastMemberOut` gains `episode_count: int | None`. It is: the aggregate count
  on show routes; the in-season appearance count for a season's guest stars;
  `null` for a season's regulars (TMDB would say "every episode", which is a
  claim, not a count) and for an episode's guests (one appearance).
- `CrewMemberOut` gains `episode_count: int | None`: aggregate on show routes,
  in-season count on the season route, `null` on the episode route.
- New `SeasonCastOut { regulars: list[CastMemberOut], guests: list[CastMemberOut] }`.
- `PersonCastCreditOut` gains `episode_count: int | None` (aggregate; null
  when the regular has no aggregate row), `seasons: list[int]` (season numbers,
  ascending, from `season_cast`) and `last_credited: date | None` (the latest
  `episode.air_date` across those seasons' episodes).
- `PersonCrewCreditOut` gains `episode_count: int | None`.
- `PersonGuestCreditOut`, `PersonEpisodeCrewCreditOut`, `PersonCreditsOut`
  keep their shape.

### 4.2 Show routes

| Route | Returns | Order |
| -- | -- | -- |
| `GET /shows/{id}/cast` | **regular credits**: distinct (person, character) in `season_cast` over the show's seasons, LEFT JOIN `show_cast` for `episode_count` and `billing_order` | `episode_count` desc nulls last, `billing_order` nulls last, person id |
| `GET /shows/{id}/guest-cast` — **new** | `show_cast` rows with no `season_cast` match on the show | `episode_count` desc, `billing_order` nulls last, id (today's order) |
| `GET /shows/{id}/crew` | **series crew** (§2.2) | `episode_count` desc, job, id (unchanged) |
| `GET /shows/{id}/episode-crew` — **new** | the remaining `show_crew` rows | same |

A regular with no aggregate row (parity gap, §3.1 (4)) still appears on
`/cast` with `episode_count: null`, sorted last. The four routes together
return every `show_cast` and `show_crew` row exactly once, plus those.

### 4.3 Season routes — new

| Route | Returns | Order |
| -- | -- | -- |
| `GET /shows/{id}/seasons/{number}/cast` | `SeasonCastOut`: `regulars` = the season's `season_cast` rows; `guests` = `episode_guest_cast` over the season's episodes grouped by (person, character), `episode_count` = the group's size, minus (person, character) pairs in `regulars` | regulars: `billing_order` nulls last, id; guests: count desc, min `credit_order` nulls last, person id |
| `GET /shows/{id}/seasons/{number}/crew` | `episode_crew` over the season's episodes grouped by (person, role), `episode_count` = the group's size | count desc, job, person id |

`number` is the season number, matching `GET /shows/{id}/episodes?season=N`.
404 when the show has no season with that number (a season is the parent
here, unlike the episodes route where it is a filter). The season row is
resolved as `get_show_seasons` resolves it — `catalog/seasons.py:deduped` —
so a duplicated season number picks the same row everywhere. Specials (season
0) are served like any other.

### 4.4 Episode routes — unchanged

`GET /episodes/{id}/guest-cast` and `GET /episodes/{id}/crew` keep their shape
and order; they gain the `episode_count: null` key from §4.1 and nothing else.
The episode page's link to the season needs `show_id` and `season_number`,
which `EpisodeOut` already carries.

### 4.5 Person route

`GET /people/{id}/credits` keeps four always-present lists:

- `cast` = **regular credits**: one entry per (show, character) with a
  `season_cast` row, with `episode_count`, `seasons`, `last_credited`; ordered
  by `last_credited` desc nulls last, show id, character id.
- `crew` = **series crew credits** (§2.2), with `episode_count`; order
  unchanged (show premiere desc, job).
- `guest_cast`, `episode_crew` unchanged.

A guest-only person on a show has no `cast` entry for it and one `guest_cast`
entry per episode. Someone regular in seasons 1–3 and a guest in season 5 has
one `cast` entry (`seasons: [1,2,3]`) and one `guest_cast` entry per season-5
episode; the SPA merges them onto one card (§5.4).

## 5. SPA (PR 3)

### 5.1 Client and types

- `CastMember.episode_count` is already declared; `CrewMember`,
  `PersonCastCredit` (`episode_count`, `seasons`, `last_credited`) and
  `PersonCrewCredit` (`episode_count`) grow to match §4.1. New `SeasonCast`
  type. The TV Maze comment on `PersonEpisodeCrewCredit` goes.
- New hooks, five-minute `staleTime`, keys in the existing style:
  `useShowGuestCast` `["show-guest-cast", id]`, `useShowEpisodeCrew`
  `["show-episode-crew", id]`, `useSeasonCast(showId, number)`
  `["season-cast", showId, number]`, `useSeasonCrew` `["season-crew", …]`.
- `usePersonCredits`'s "never re-sort" comment is amended: the lists are
  consumed in API order; the *cards* are ordered by §5.4.
- MSW: handlers for the five new routes; fixtures with a regular who also
  guested, a guest-only show, a cast-and-crew show, a regular with
  `episode_count: null`, and a season with one regular, two guests (one with
  two appearances) and crew.

### 5.2 Episode page (`EpisodePage.tsx`)

Tabs, gating, defaults and deep links stay as NEU-1209 built them. One
addition: directly above the credits region (rendered whether or not the
region is), a single line linking to the season's cast — "Regular cast for
Season 3" → `/shows/{show_id}/episodes?season=3&tab=cast`. Season 0 reads
"Regular cast for Specials". Chip meta in the Guest cast panel stays empty.

### 5.3 Show page (`ShowDetailPage.tsx`)

- Tabs, in order: **Seasons**, **Cast**, **Guest stars**, **Crew**, **Episode
  crew**, **Similar**; `?tab=` values `cast`, `guest-stars`, `crew`,
  `episode-crew`, `similar`; fallback to Seasons for anything else or an empty
  requested tab. Disabled-when-empty with a zero count, enabled while loading
  or on error, as the current four. Six triggers scroll horizontally at phone
  width as the person page's strip does.
- Cast and Guest stars render `CastList` with the "N episodes" meta (which
  now arrives). `CastList`'s collapse changes from "12 then all" to **"12, then
  Show more in pages of 48"** so Law & Order's 11,517 guest stars do not mount
  at once; Cast (tens of rows) rarely reaches the second page. Crew and
  Episode crew render `CrewList` (grouped by role, in API order, its existing
  cap and "Show all").

### 5.4 Person page (`PersonPage.tsx`, `personCredits.ts`)

- Tabs: **Cast** and **Crew**, hidden when empty. `?tab=crew` selects Crew;
  `?tab=cast`, no value, and the old `?tab=guest` select Cast; the old
  `?tab=episode-crew` selects Crew. Fallback to the first populated tab, URL
  replace-not-push, "No credits yet" when all four lists are empty: unchanged.
- **One card per show per tab**, built from `cast` + `guest_cast` (Cast) or
  `crew` + `episode_crew` (Crew) for the same `show.id`. Card header: show
  link, premiere year, and **the count after the name** — for Cast the sum of
  the show's regular credits' `episode_count` (which upstream already counts
  guest turns into), or, when the show has no regular credit, the number of
  guest episodes; for Crew the same with series crew counts and episode crew
  rows. Under the header, the distinct character (or role) labels joined
  with " · ".
- **Expandable list** under the header, collapsed by default, with a "Show
  more" past ten rows:
  - one row per regular season, ascending — "Season 3 · 22 episodes", linking
    to `/shows/{id}/episodes?season=3&tab=cast` (episode count from the
    show's seasons, which `useShow` already loads on this page's neighbours;
    fetch `GET /shows/{id}` on expand if needed rather than up front);
  - one row per guest / episode-crew episode, newest first, as
    `EpisodeGroupCard` renders them today ("S5E3 — title", labels).
  A show with only one row renders it inline in place of the disclosure, as
  `EpisodeCreditCard` does for a single episode today.
- **Card order:** by the latest of `last_credited` (regular credits) and the
  newest episode `airdate` among the card's episode rows, newest first, nulls
  last, ties by show id. Client-side.
- **Tab counts are cards (shows)**, not credits — a departure from NEU-1211,
  which counted credits because the tabs were per grain; with one entry per
  show, the show count is the number the user sees.
- The collapse at 12 cards with "Show all N shows" stays.

### 5.5 Season page (`EpisodesPage.tsx`)

- A tab strip under the season header — **Episodes** (default), **Cast**,
  **Crew** — sharing the page's `?season=` and adding `?tab=cast|crew`
  (anything else → Episodes; changing season keeps the tab). Cast and Crew
  disabled with a zero count when their queries resolve empty; Episodes is
  never disabled. Tab changes replace the URL.
- **Cast panel:** two `CastList`s under one hidden panel heading — "Regular
  cast" (billing order, no meta) and "Guest stars" (with the "N episodes"
  meta, which here means appearances in this season). Either list absent
  when empty; the panel is disabled only when both are.
- **Crew panel:** `CrewList`-style grouping by role in API order, "N
  episodes" meta.
- The page's existing tests (default season, `?season=2`, picker) must keep
  passing with the Episodes tab default.

## 6. Order of work and rollout

1. **PR 1 (backend):** probe script and its recorded answer; payload classes;
   migration; writer; fetch route per §3.2; backfill CLI and Taskfile targets;
   tests (§7.1). Merge, deploy, run the backfill in production by hand, record
   the run in `docs/migration/README.md`.
2. **PR 2 (backend):** routes and schemas (§4), tests (§7.2). Merge and deploy
   only once `backfill:season-credits:report` shows zero remaining, so no user
   sees a show whose regulars are all in Guest stars because its season
   credits have not landed. The SPA is untouched by PR 2: it reads only routes
   that still exist, and new keys on existing routes are additive.
3. **PR 3 (frontend):** §5, tests (§7.3).

## 7. Acceptance criteria

### 7.1 Ingest

1. The probe ran against live TMDB and its four answers are in PR 1's
   description; §3.2's route is chosen accordingly and named in the PR.
2. `tests/unit/tmdb/test_api_payloads.py`: a season detail with `credits`
   parses `cast[]` into `TMDBSeasonRegular` with `billing_order` from `order`;
   without the key `credits is None`; with `credits: {cast: []}` it is an empty
   list; `crew` is ignored; an entry with no person parses (or fails, if §3.1
   measured strict).
3. Route A only: `plan_append` emits `season/N,season/N/credits` pairs and 4
   season slots; `SPECULATIVE_SEASONS == (0,1,2,3)`; a forty-season show
   overflows 37; `get_tv_season(..., append=("credits",))` sends
   `append_to_response=credits`; `_collect_appended_seasons` attaches
   `season/N/credits` to season N and never parses it as a season. Route B
   only: `fetch_series_with_seasons` makes one `/season/{n}/credits` request
   per season of the show, after the overflow fetches, and a failing one fails
   the show.
4. `tests/integration/tmdb/test_upsert.py`: a season's regulars are written
   one row per (person, character) with the season's billing order; the
   character interns **per show** and is the same row a guest or show-cast
   entry with that name uses; a second pass with a regular removed removes
   the row; a payload without the key keeps existing rows; `cast: []` clears
   them; a season whose entries all lack a person keeps its rows; a
   duplicated entry is written once; a blank character is stored null and two
   blank-character entries for one person on one season do not conflict
   (`NULLS NOT DISTINCT`); re-fetching one season leaves the others' rows.
5. `tests/integration/catalog/test_catalog_credit_tables.py`: the unique key,
   the cascade from `season`, and the FK to `person`.
6. `tests/integration/tmdb/test_season_credits_backfill.py`, mirroring
   `test_credits_backfill.py`: writes `season_cast` for every season of a show
   including overflow ones; stamps `season_credits_synced_at`; a season that
   returned no `credits` leaves the show unstamped; a show with regulars in no
   season is stamped; skips shows already stamped; resumable across two runs;
   a 404 is `gone` and not stamped; the abort threshold; no partial writes for
   a failed show; `report` totals.
7. `mark_series_synced` stamps `season_credits_synced_at`; the full-pass and
   delta tests that pin the stamped columns grow the fourth.

### 7.2 Contract

8. `test_credits_routes.py`, seeded with `SeasonCast` rows as well as the
   four existing tables: `/shows/{id}/cast` returns exactly the (person,
   character) pairs with a `season_cast` row on the show, once each, with the
   aggregate `episode_count`, in the §4.2 order; a regular with no `show_cast`
   row appears with `episode_count: null`, last; a regular's guest rows on
   another season do not put them on `/guest-cast`; `/guest-cast` is the
   remaining `show_cast` rows in episode-count order; the two together are
   every `show_cast` row once; `/crew` ∪ `/episode-crew` is every `show_crew`
   row once with the §2.2 split (no episode rows → crew; equal count →
   episode-crew; greater → crew); every entry carries `episode_count`.
9. Season routes: `regulars` in billing order; `guests` grouped with counts
   and ordered by count; a season regular who also has a guest row in that
   season appears in `regulars` only; a guest of another season is absent;
   crew grouped by (person, role) with counts; unknown season number 404s;
   season 0 works; a duplicated season number resolves to the same row the
   seasons route picks; cache header and auth as the siblings.
10. Episode routes: unchanged assertions pass, plus `episode_count: null` in
    the entry shape.
11. `test_people_routes.py`: `cast` holds one entry per (show, character)
    with a `season_cast` row, with `seasons` ascending, `episode_count` and
    `last_credited` = the latest air date over those seasons' episodes; a
    guest-only show is absent from `cast` and present in `guest_cast`;
    regular-then-guest yields one `cast` entry and the guest episodes; order
    by `last_credited` desc nulls last; `crew` holds series crew only with
    `episode_count`; four keys always present; four empty lists for a person
    with none.
12. Plan check recorded in PR 2's description: `EXPLAIN (ANALYZE, BUFFERS)`
    of `/shows/562/cast`, `/shows/562/guest-cast`, the season cast query for
    Law & Order season 1, and `/people/21499/credits` on the workspace
    database, each under 50 ms and on existing indexes plus
    `ix_season_cast_person_id`.

### 7.3 SPA

13. `EpisodePage.test.tsx`: the season link renders with the right season
    number and `?tab=cast`, including on an episode with no credits region,
    and reads "Specials" for season 0; NEU-1209's assertions still pass.
14. `ShowDetailPage.test.tsx`: six tabs in order with counts; `?tab=guest-stars`
    and `?tab=episode-crew` deep links; disabled when empty; Cast rows show
    "N episodes"; a 60-entry Guest stars fixture shows 12, then 48 more per
    "Show more".
15. `EpisodesPage.test.tsx`: three tabs, Episodes default; `?tab=cast` shows
    "Regular cast" then "Guest stars" with appearance counts; `?tab=crew`
    grouped by role; empty Cast/Crew disabled; the tab survives a season
    change; the three existing tests pass.
16. `PersonPage.test.tsx`: two tabs; `?tab=guest` → Cast, `?tab=episode-crew`
    → Crew; one card for a regular-then-guest show with the count, the
    character labels, season rows linking to the season cast and episode rows
    beneath; a guest-only show counts its episodes; a cast-and-crew show has a
    card in each tab; card order follows the latest date; tab counts are
    shows; "No credits yet"; the 12-card collapse; the NEU-1210/1211
    accessibility tests still pass.
17. `personCredits.test.ts`: two-list grouping, the card count rule, the card
    date, the season-row builder.
18. `CastList.test.tsx`: the paged "Show more"; `CrewList.test.tsx` and
    `EpisodeGuestCast.test.tsx` updated for the meta rules.
19. `pnpm typecheck` and `pnpm lint` clean; no reference to `?tab=guest` /
    `?tab=episode-crew` remains outside the alias handling.

## 8. Docs this changes

- `CONTEXT.md` — done in the design session (see header).
- `.claude/docs/architecture-endpoints.md` — five new routes, the narrowed
  meaning of `/shows/{id}/cast`, `/crew` and the person `cast` / `crew` lists,
  `episode_count` on every credit entry.
- `.claude/docs/patterns-tmdb-ingest.md` and `patterns-migration.md` — season
  credits: which route the probe chose, the None-vs-`[]` rule at season grain,
  the backfill and its watermark.
- `src/tvbf/catalog/models.py` — `SeasonCast` docstring in the house style
  (what it is, why three-part unique, why per-show characters); a sentence on
  `ShowCast` that "regular" is decided by `season_cast`, not by this row.
- `docs/migration/README.md` — the production backfill run, date and totals.
- `docs/specs/NEU-1031-tmdb-coverage-audit.md` — a note beside the skipped
  `credits` row that season credits were adopted by NEU-1512.
- `tvbf-frontend/docs/specs/NEU-1512-list-full-episode-cast.md` — pointer.
  NEU-1209, NEU-1210, NEU-1211 and NEU-1007 each get a one-line note that
  NEU-1512 reshaped the tabs or grouping they describe.

## 9. Out of scope, deferred

- **Season-level crew** from `season/{n}/credits.crew[]` — not ingested (§2.2).
- **Per-episode presence of regulars** — TMDB does not have it; the episode
  page links to the season instead (§2.1).
- **Server-side pagination of credit routes** — the largest response today is
  11,550 rows and the SPA pages it; revisit if a route's size, not its query,
  becomes the cost.
- **Tombstone filtering on credit routes, `total_episode_count` in the API,
  a person-page filter by show** — unchanged from today.
- **A fallback for shows whose season credits have not landed** — handled by
  ordering PR 2 after the backfill (§6), not by code.
- **The 0.7% of guest-only (show, person) pairs with no `show_cast` row** stay
  off the show page's Guest stars tab, which partitions `show_cast`; they are
  on the episode, season and person pages, which read `episode_guest_cast`.
