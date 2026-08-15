# TMDB replaces TV Maze as the catalog source

**Status:** accepted (2026-08-08)

TV Maze stops being the spine. The catalog mirror is rebuilt from TMDB, user data is
migrated onto it, and the `tvmaze` schema is retired. TMDB is not a supplement and not
a second axis — it is the source.

This reverses the position held as recently as the TMDB Discovery project description,
which assumed TMDB would supplement TV Maze and listed replacement as a question to
defend rather than adopt.

## What the numbers said

Measured 2026-08-08 against the local mirror and prod.

**TV Maze's weakness is concentrated exactly where the product wants to grow.** Of
88,971 mirrored shows, only 74% carry any external id (61,999 TVDB, 47,907 IMDb). Broken
out by status, the gap tracks recency:

| Status | Shows | With a join key |
|---|---|---|
| Ended | 67,713 | 76.8% |
| To Be Determined | 5,038 | 76.7% |
| Running | 14,303 | 65.2% |
| In Development | 1,917 | **31.6%** |

TV Maze lists **77 shows in total with a future premiere date**. Discovery surfaces —
similar, trending, most anticipated — all lean on exactly the forward-looking region
where the catalog is thinnest.

**TMDB is 2.57× larger.** The daily id export (`tv_series_ids_08_07_2026.json.gz`)
contains 228,611 TV series against our 88,971.

**Acquisition economics are not comparable.** TV Maze allows 18 req/10s (1.8/s); TMDB's
ceiling is ~40–50/s. Beyond raw rate, `append_to_response` returns a show plus its
`external_ids`, `alternative_titles`, `aggregate_credits` and individual seasons with
full episode lists in a single request. The TV Maze equivalent took roughly 102 hours
across separate passes. The TMDB equivalent is on the order of **3.2 hours for the entire
catalog including credits**.

**The migration window is open and closing.** Prod holds 5 users, 563 distinct shows
touched by user activity, and 8,499 episode watch rows. Of those 563 shows, **4 have no
external id at all**, and 3 of those have zero episode watches. The human-matching
residue for a lossless migration is a single-afternoon task today. It will not stay that
way.

## Why

**The reason to hold TV Maze was never its data — it was switching cost, and switching
cost is currently near zero.** Two of the three arguments originally made against
replacement dissolved under measurement: user-data migration is 8,499 rows rather than a
projection, and re-acquisition is hours rather than the ~102 unrepeatable ones that shaped
this codebase's entire ingest philosophy. That philosophy is a property of TV Maze's rate
limit, not of the problem.

**TMDB supplies capabilities TV Maze structurally lacks**, and they are the ones the
roadmap is made of: behaviour-based recommendations, a trending feed, and watch providers.
These are not columns we could backfill; they are products of TMDB having user accounts
and engagement data.

## What this costs, accepted knowingly

**Character stops being globally identified.** TMDB models character as free text on a
credit. We intern per show instead (see CONTEXT.md). Measured impact: of 1,509,298
characters in prod, 2,621 are played by more than one person — all preserved by per-show
interning — and exactly **one** spans more than one show.

**Show and episode crew roles stop being two vocabularies.** ADR-0003 gave episode crew
its own interned lookup, `episode_crew_role`, because TV Maze's two vocabularies were
disjoint — `Writer` / `Director` / `Story` / `Teleplay` at episode grain against 233
production-function names at show grain. TMDB emits the same `(department, job)` pair at
both grains, and measured (`scripts/probe_tmdb_credit_shapes.py`, 5 series, 2026-08-11)
**all 78 episode-level pairs also appear at show level — 100% overlap**. So `catalog` has
one `crew_role` covering both, and **ADR-0003's separate-lookup paragraph is superseded for
`catalog`**; it continues to describe `tvmaze`, which keeps its two tables until cutover.

This is a widening rather than a loss — nothing that was distinguishable stops being so —
but it is recorded here because the distinction was a deliberate decision once, and the
reason for it did not survive the source change. A crew role also stops being a single
string: `department` is what a person page groups by, and it is a closed vocabulary where
`job` is a long tail.

**`schedule` (broadcast day and time) has no TMDB equivalent** and is lost.

**`network` and `web_channel` merge.** TMDB carries a single `networks` concept plus
`watch/providers`. Browse filters on both today and will need reshaping.

**Long-tail coverage is not proven.** 228,611 > 88,971 is a count, not a guarantee that
TMDB holds our 4,536 Russian and 3,243 Chinese entries. This risk is *mitigated but not
eliminated* by a pre-cutover go/no-go comparison over the populated `catalog` schema. A
deliberate decision was made not to gate the project on measuring it first.

## Consequences

The new catalog lives in a **`catalog` schema, named for what it holds rather than where
it came from** — the source has now changed once and the name should not have to change
with it. User tables reference **internal surrogate keys** (ADR-0008), which is what makes
"no user data loss, period" a property of the table definitions rather than of process.

ADRs 0001, 0003, 0004, 0005 and 0006 describe the TV Maze spine and become historical
once it is retired. Their *patterns* port and should: the tombstone floor guards
(ADR-0005) apply unchanged to a reverse diff against TMDB's daily export, the
cross-process request budget (ADR-0006) generalises to a second upstream, and
prune-on-authoritative-payload (ADR-0004) is the same rule against a different embed.

ADR-0002 stands and tightens: TMDB is mirrored ahead of time, never called from a request
path. Its image exception now covers a second CDN (`image.tmdb.org`).

Nothing is deleted at cutover. `DROP SCHEMA tvmaze` is a separate, later, explicitly
approved step, so the archive and the intact TV Maze mirror remain as two independent
recovery paths through the risk window.
