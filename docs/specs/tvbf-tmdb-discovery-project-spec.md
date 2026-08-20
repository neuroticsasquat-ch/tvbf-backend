# TVBF: TMDB Discovery — project spec

**Status:** approved 2026-08-08; **substantially revised 2026-08-16** against live measurement
**Blocked by:** [TVBF: TMDB Migration](tvbf-tmdb-migration-project-spec.md) — now complete
**Decision record:** [ADR-0002](../adr/0002-no-upstream-api-in-request-path.md),
[ADR-0007](../adr/0007-tmdb-replaces-tvmaze-as-the-catalog-source.md)

## Purpose

Three discovery surfaces sourced from TMDB: **similar shows** on the show detail page,
**trending**, and **most anticipated**.

This project got substantially smaller when TMDB became the catalog source (ADR-0007).
Everything that was hard about it — id mapping, join coverage, rendering entries with no
local counterpart — was an artifact of TMDB being a *second* source. On a TMDB spine,
every TMDB id is already a `catalog.show`.

## What the 2026-08-16 revision changed, and why

The 2026-08-08 draft was written before anyone called the endpoints. Four of its decisions
did not survive contact with the live API and the production mirror. Every number below is
measured; §7 records the method so a future reader can re-run it rather than trust it.

1. **The `/similar` fallback is deleted.** It was specified to rescue the long tail. It
   rescues nothing: in a 36-show sample it returned **zero results for all three shows
   where `/recommendations` also returned zero**, and where it does return something the
   results are noise.
2. **The `vote_count` floor on most anticipated is deleted.** Of 408 future-dated shows in
   production, **4 carry any votes at all and 1 carries ten or more**. The floor as
   specified would have produced a list of one row.
3. **Most anticipated becomes a local query with no upstream call and no snapshot job.**
   `/discover/tv` and plain SQL over `catalog.show` return the same list.
4. **Similar shows rides `append_to_response` rather than a recurring pass.** One namespace
   on the fetch the ingest and the nightly delta already make, so after a single backfill
   the surface costs nothing to keep current, forever.

## The rule that governs all three surfaces

**If a user cannot click it and add it to My Shows, it does not appear.** A dead card in a
tracking app is a tease, not information. Entries that cannot be resolved to a
`catalog.show` are dropped, on every surface, without exception.

An earlier draft argued for an asymmetry — dropping on similar shows but showing
non-clickable cards on trending, on the grounds that omitting a trending show makes the
list factually wrong. That was rejected: nobody has TMDB's list memorised, so a shorter
list costs nothing visible, while an unclickable card costs a real moment of "why is this
broken?"

**Measured, the rule is a no-op, and it stays anyway.** 502 of 502 distinct recommendation
targets across 30 source shows resolved to a `catalog.show`, none tombstoned, none adult;
20 of 20 trending ids resolved. That is what a TMDB spine buys, and it is exactly why the
rule must remain as a *guard* rather than be deleted as dead code — a show TMDB created
this morning is not in our mirror until tonight's delta, and the delta can fail.

## ADR-0002 applies without exception

Everything a user request reads is already in Postgres. All three surfaces are **derived
lists**, not entity lookups — nobody needs "TMDB's record for show 1399 on demand," they
need "the twelve shows similar to this one."

There is **no response cache**, TTL'd or otherwise. The word "cache" appearing in earlier
versions of this project's description predates ADR-0002 and is retired.

---

## 1. Popularity refresh from the daily export — the shared prerequisite

Two of the three surfaces rank on `catalog.show.popularity`, and today that column is
**frozen for 97% of the catalog**. 200,698 of 229,443 shows were last synced 2026-08-10
and 21,926 on 2026-08-11; the nightly delta touches only 1,300–2,300 a day, because
`/tv/changes` reports *content edits* and TMDB recomputing a popularity score is not one.
Popular shows stay fresh incidentally — the top five trending shows were all re-synced the
same day — but the tail drifts without bound, and a ranked list built on mixed-vintage
scores is wrong in a way nothing surfaces.

**The fix is free and already downloaded.** `tmdb/export.py` fetches the daily id export
for the ingest work list and the tombstone reverse diff. Each of its 229,150 lines carries
popularity:

```
{"id":1,"original_name":"プライド","popularity":3.8449}
```

`parse_series_ids` currently discards everything but `id`. Widening it to return
`(id, popularity)` and bulk-updating `catalog.show.popularity` costs **no request, no rate
budget and no credential** — the file is on `files.tmdb.org`, which is why that module
builds its own unauthenticated client in the first place.

**Rules.**

- **The refresh writes `popularity` and nothing else.** It is not an ingest and must not
  become one; the export carries no other field we store.
- **It runs at the end of the nightly catalog delta**, where the export is already in hand.
  A second download for a second job would double a 5 MB transfer for nothing.
- **It reuses `TruncatedExportError` and the existing floor guards.** A short file must not
  be allowed to zero out popularity across the mirror any more than it may tombstone it.
- **It does not touch `tmdb_synced_at` or `credits_synced_at`.** Popularity arriving from
  the export is not evidence that a payload was mirrored, and stamping either watermark
  would retire shows from a work list they belong on. This is the same distinction
  NEU-1127 drew when it had to add a second watermark rather than reuse the first.

---

## 2. Similar shows

**One endpoint: `/tv/{id}/recommendations`. There is no fallback.**

TMDB has two and the 2026-08-08 draft picked both, with `/similar` filling in below six
results. The measurement kills that arrangement on its own terms.

`/recommendations` **is very nearly binary, and the exception does not change the
decision.** Across 36 sampled production shows it returned **20 results or 0 results,
never "a few"** — including 20 results for 9 of 12 zero-vote long-tail shows. The draft's
premise ("behaviour-based recommendations require behaviour, so an obscure show with 40
votes returns few results or none") is half right: the failure is overwhelmingly total
rather than partial.

**Corrected 2026-08-16 against a larger sample.** NEU-1052's 100-show production smoke run
— the hundred *most popular* shows in the mirror — stored 20 rows for 95 of them and fewer
for five: Coronation Street 19, Goede Tijden Slechte Tijden 18, Highly Questionable 4,
The Tonight Show Starring Johnny Carson 3, Tagesschau 1. None of the shortfall was ours:
`targets_dropped` was 0, so every entry TMDB offered resolved to a mirrored show. So a
partial list is real at roughly **5%**, and **three of those five are below six** — the
"fewer than 6" case this section said "describes a case that does not occur" does occur.

That retires the *phrasing* and none of the reasoning. The `/similar` fallback stays
deleted, because it was rejected on the two paragraphs below rather than on this one: it
returns zero in exactly the places recommendations does, and noise where it answers. Note
what the five have in common — rolling news, a nightly talk show, a daily sports panel and
two continuing soaps. These are formats with thousands of episodes and no plot to be
similar *to*, which is a much more specific failure than "obscure", and is why the
long-tail premise was wrong in the first place.

**The open question this leaves is a read-path one**, and it belongs to NEU-1053: §2 says
top 12 after filtering and sets no *minimum*, so a one-card "More like this" section under
Tagesschau is what the rules as written produce.

`/similar` **fails in exactly the same places.** It returned zero for **all three** shows
where recommendations returned zero, and zero for 15 of 36 overall. It cannot be a fallback
for a failure it shares.

And where it does answer, it answers badly. TMDB's own staff call it *"an old, and largely
not that great method"*; the sample agrees — Game of Thrones returns *The Shining*, *Miss
the Dragon*, *Friend to Fan*; The Rings of Power returns *The Beginning After the End*,
*My Dear Brother*. It reports 61,933 "results" for Game of Thrones, which is another way of
saying it is barely filtering. Under a "More like this" heading that is worse than an
absent section, which is what §2's degradation rule already delivers for free.

### Delivery: one namespace on a fetch we already make

`append_to_response=recommendations` was verified live and returns the full 20-entry first
page inside the series payload. So `recommendations` joins `DEFAULT_APPEND`, taking it from
eleven namespaces to twelve.

**The cost is one speculative season slot, and it is 0.50 percentage points.** Namespaces
and `season/N` entries share the same twenty (`APPEND_TO_RESPONSE_LIMIT`), so the
speculation window narrows from seasons 0–8 to 0–7. Measured across all 210,343 mirrored
shows carrying ingested seasons: **96.84%** fit inside 0–8, **96.34%** inside 0–7. That is
1,054 additional shows paying one extra `get_tv_season` each — roughly two and a half
minutes on a pass that runs most of a day. `SPECULATIVE_SEASONS` is already derived from
`APPEND_TO_RESPONSE_LIMIT - len(DEFAULT_APPEND)`, so the window narrows automatically and
nothing overflows into a hard 400.

**The coverage-audit tripwire must be updated by hand, because it will not fire.**
`tests/unit/catalog/test_audit_coverage.py` lists `recommendations` in `SKIPPED` and
asserts the name appears nowhere in the `catalog` surface — the docstring calls adding one
back "a decision, and should read like one." But the assertion is an **exact** match against
table and column names, and neither `show_recommendation` nor `recommendations_synced_at`
equals `recommendations`. So the guard sails past exactly the reversal it was written to
catch. NEU-1052 moves the entry out of `SKIPPED` and into the modelled inventory
deliberately, and records the reversal in the same commit; it does not get to happen by
the test staying green.

*(This supersedes NEU-1031's judgement that `recommendations` is "TMDB-computed and
volatile rather than a catalog fact". That reasoning was about what belongs in a catalog
mirror, and it was right on its own terms — but the same paragraph warns that "every
namespace left off is a field that becomes a multi-hour backfill later, which is exactly
how the TV Maze mirror accumulated four of them." This project is that backfill arriving on
schedule. Riding the append is what stops it arriving a second time.)*

**Consequence: after one backfill, the surface never costs another request.** Every future
ingest and every nightly delta re-fetch refreshes recommendations as a side effect, so
there is no recurring refresh pass, no refresh cadence to choose, and no staleness rule.
The draft's "monthly is a sane default" is retired along with the job it paced.

### The one-time backfill

The 229,418 already-synced shows carry no recommendations, for the same reason 228,841 of
them carried no credits after NEU-1127: they were mirrored before the writer existed.

- **Watermark: `catalog.show.recommendations_synced_at`.** A third column beside
  `tmdb_synced_at` and `credits_synced_at`, added for the third time for the second reason —
  a "has no `show_recommendation` row" predicate cannot distinguish *upstream returned
  none* from *nobody has asked*, and a zero-result show is 8% of the long tail, so that
  predicate would never converge. `mark_series_synced` stamps all three (every caller
  reaches it through `mirror_series`, which now appends the namespace by construction);
  the backfill stamps only its own.
- **Ordered `popularity DESC`, resumable, stoppable.** The pass is ~8.8 hours at the
  measured 7.27 shows/sec, but the ordering front-loads all of the value: the top 20,000
  shows take about 45 minutes and cover essentially every page a user will load. **Stopping
  early is a supported outcome, not a failed run** — the watermark makes a partial pass
  indistinguishable from a paused one, and the surface degrades to "no section" for shows
  nobody visits.
- **It writes recommendations and nothing else**, through a `write_series_recommendations`
  seam, on the `write_series_credits` precedent — so "writes no spine" is a property of the
  writer rather than a promise made by a caller reaching past the underscores.
- **Re-run it after any later full ingest**, on the same footing as `season_dedupe` and
  `orphan_retire`.

### Storage

`catalog.show_recommendation(source_show_id, rank, target_show_id)`.

- Ranks preserved from TMDB's response order.
- Store **20**, display **12**. The headroom absorbs read-time filtering.
- **No `source` column.** With one endpoint there is one provenance and a column holding a
  single constant is a lie waiting for a second writer that this design says will not come.
  If the Personalized Recommendations project later adds a "people who watched this also
  watched" signal, that is a different table with a user dimension, not a second value here.
- The whole list for a source show is **replaced** on refresh, not merged — TMDB's ranking
  is a total order and a partial update would interleave two vintages of it.

### Read path

`GET /shows/{id}/similar` returns the existing `ShowSummary` shape, so `ShowCard` /
`ShowGrid` are reused unchanged. Top 12 after filtering `deleted_upstream_at IS NOT NULL`
and `adult` at read time — the same read-time filter NEU-1108 draws for weekly
recommendations, and for the same reason: a list computed in March can name a show
tombstoned in June, and a write-time copy would make a resurrected show permanently
invisible.

**Degradation: a show with no rows renders no section at all** — no empty state, no error,
no spinner. Measured, this affects roughly 8% of zero-vote long-tail shows and 0 of 24
sampled tracked and mid-tier shows.

### Scope boundary

This blends none of our own watch data. "People who watched this also watched" from
`app.user_episode_watch` is a better signal for our users and belongs to **TVBF:
Personalized Recommendations**.

### On the word "user" in "built from user rating and favourite data"

That is **TMDB's** users, on themoviedb.org — *"the very same method used on the website."*
The list is a property of the source show and is **identical for every viewer**; the table
has no user dimension and the endpoint takes no user parameter. The phrase read ambiguously
in the 2026-08-08 draft and this paragraph exists to stop it reading that way again.

---

## 3. Trending

**`/trending/tv/week`, one request a day, 20 entries, stored as a snapshot with
`captured_at`.** This is the only surface that still calls an endpoint on a schedule.

`week` over `day`: daily trending is news-reactive — one cast controversy spikes a show for
24 hours — and the job runs daily regardless, so `day` buys volatility rather than
freshness. `week` matches the decision the surface feeds, which plays out over weeks.

**It is not reproducible locally and we did check.** Trending is a velocity signal over
view and search counts we do not have, and it is demonstrably not a popularity sort:
*Our Sticky Love* (popularity 165) ranks below *Lanterns* (popularity 56) in the live list.
A home-grown approximation from seven-day popularity deltas is constructible now that §1
refreshes popularity daily, but it would be an unvalidatable tuning project undertaken to
save **one request per day**. Rejected on that arithmetic alone.

**Staleness is the server's rule.** Store `captured_at` and return an empty list once the
snapshot exceeds **7 days**. The SPA must never re-implement it — a rule enforced in two
places drifts, and the failure mode is week-old data under a label reading "trending right
now". Silent staleness under a present-tense label is worse than an absent section, and
this is one of the few places where the data has an expiry baked into its own name.

**A failed run leaves the previous snapshot intact**, and the staleness cutoff is what
bounds how long that is allowed to matter.

**No personalisation.** Shows the user already tracks are **marked, not filtered** —
trending is a claim about the world, and seeing your own show trending is a feature.

---

## 4. Most anticipated

**A live SQL query over `catalog.show`. No upstream call, no snapshot table, no job.**

```sql
SELECT … FROM catalog.show
WHERE deleted_upstream_at IS NULL
  AND NOT adult
  AND first_air_date >= current_date
  AND first_air_date < current_date + <window>
ORDER BY popularity DESC NULLS LAST
LIMIT <length>
```

### Why local, when the draft said `/discover/tv`

Because they return the same list, and one of them is free. Run side by side on 2026-08-16,
`/discover/tv?first_air_date.gte=…&sort_by=popularity.desc` and the query above agree on
every show in the top fifteen and differ only in ordering — and the ordering differences are
entirely explained by our popularity being six days stale, which §1 fixes:

```
TMDB /discover   VisionQuest, Por Você, Crew Girl, Neagley, Harry Potter,
                 S.W.A.T. Exiles, You Maniac, Neuromancer, The Dynasty: UConn Huskies,
                 Last Seen, Stronger Together, Carrie, Monster: The Lizzie Borden Story…
local SQL        VisionQuest, Por Você, Crew Girl, Neagley, Harry Potter,
                 Neuromancer, S.W.A.T. Exiles, You Maniac, The Dynasty: UConn Huskies,
                 Mr. Fanboy, Stronger Together, Last Seen, Carrie, Monster: … Lizzie Borden…
```

TMDB reports 396 matching series; we hold 408. This is what ADR-0007 bought and it would be
odd not to spend it: `/discover/tv` is a query against TMDB's catalog, and TMDB's catalog is
our catalog.

Three things follow from it being a query rather than a snapshot, and each one deletes a
problem the draft had to solve. **There is no "drop anything that has premiered since the
snapshot was taken"** — `current_date` is evaluated on the read. **There is no "a failed run
leaves the previous snapshot intact"** — there is no run. **There is no staleness rule**, of
the kind trending needs, because nothing is stored.

### The `vote_count` floor is deleted

It was specified as "the main defence against an endless tail of low-signal listings." It is
instead a defence against the surface existing. Of 408 future-dated shows in production,
**4 have any votes and 1 has ≥10**; every entry in both top-20 lists above has `vote_count
= 0`. Unpremiered shows do not get voted on — that is what "unpremiered" means — so a vote
floor on this surface is a category error, not a threshold needing tuning.

**Nothing replaces it, because the tail does not need defending.** Ranks 21–45 read
*The Drop: A Snowfall Saga*, *Avatar: Seven Havens*, *Blade Runner 2099*, *Crystal Lake*,
*Ben-Hur*, *A Tale of Two Cities*. Popularity is already doing the filtering, because a show
nobody has heard of does not accumulate a popularity score either.

### Parameters

Two, both config, neither load-bearing:

- **Window** — default **365 days**. Barely binds: 385 of 408 future-dated shows fall inside
  a year, so its real job is excluding placeholder entries dated far out (*Ben-Hur*, 2027).
- **Length** — default **24**. Quality holds well past 20, so this is a page-layout decision.

**An undated show never appears.** 2,501 shows carry `status IN ('Planned','In
Production','Pilot')` with no `first_air_date`, and there is no defensible position to sort
them into. `status` is deliberately **not** in the predicate: *Lanterns* is `Returning
Series` with a future first air date and belongs on the list.

### Performance

45 ms unindexed on the production mirror (parallel sequential scan, top-N heapsort, 408
rows surviving the filter) — already acceptable for a browse route carrying
`Cache-Control: public, max-age=300`. A btree on `first_air_date` filtered to
`deleted_upstream_at IS NULL` makes it a range scan. `current_date` is *stable* rather than
*immutable*, so it cannot appear in a partial-index predicate; index the column, not the
comparison.

---

## 5. Attribution

TMDB requires the notice **"This product uses the TMDB API but is not endorsed or certified
by TMDB"** placed prominently in an About or Credits section, plus an approved logo *"less
prominent than the logo or mark that primarily describes the application"*, linking to
themoviedb.org, referring to the service only as "TMDB" or "The Movie Database".

**It goes in the existing sitewide footer** (`AppShell.tsx`), which is where the TV Maze
CC BY-SA notice sat until NEU-1147 removed it. A sitewide footer is a stronger placement
than a buried About page, and there is an established pattern to match. No new page.

**No per-surface attribution.** It is not required and it is clutter.

**The logo ships as a local asset.** ADR-0002's image exception covers *content* images
hot-linked to a CDN; a brand mark in our own chrome is different.

---

## 6. Out of scope

Personalised recommendations of any kind — "because you watched", trending among your
connections — derive from our own watch data and live in **TVBF: Personalized
Recommendations**.

A home-grown trending signal from popularity deltas (§3). §1 makes it *possible*; nothing
makes it *worth it* at one request per day.

A local keyword/genre similarity as a second tier under §2. It was considered and measured:
only **67,095 of 229,443 shows (29%)** carry any keyword, and genre alone is not a
similarity signal. It scores well on the shows that matter — 542 of 563 tracked shows carry
keywords — but it cannot claim the full-coverage role that would justify a second mechanism,
and `/recommendations` already answers for 92% of the long tail.

---

## 7. Milestones and ticket impact

1. **Popularity refresh (§1)** — new, and a prerequisite for milestone 4. Widen the export
   parser, bulk-update `catalog.show.popularity` at the end of the nightly delta.
2. **Similar shows (§2)** — `recommendations` into `DEFAULT_APPEND`, the
   `recommendations_synced_at` watermark, `catalog.show_recommendation`, the ordered
   one-shot backfill, and `GET /shows/{id}/similar`.
3. **Trending (§3)** — daily `/trending/tv/week` snapshot with the 7-day server-side cutoff,
   `GET /trending`, and the Discover page shell.
4. **Most anticipated (§4)** — `GET /anticipated` as a live query, plus its tab.

**Existing tickets needing revision:**

| Ticket | Change |
|---|---|
| NEU-1052 | Rewritten: append-based delivery, `/similar` and `source` deleted, watermark + ordered backfill added, "monthly refresh" retired |
| NEU-1053 | `source` no longer exists; add the `adult` read-time filter |
| NEU-1054 | Unchanged; the empty-section rate is now measured (~8% of long tail, 0 of 24 tracked/mid) |
| NEU-1055 | Unchanged |
| NEU-1056 | Unchanged |
| NEU-1057 | Unchanged |
| NEU-1058 | **Largely dissolved** — no snapshot job. Reduced to the index plus the window/length config; the vote floor is deleted |
| NEU-1059 | Live query rather than snapshot read; the "drop already-premiered" clause is now structural |
| NEU-1060 | Unchanged |
| *(new)* | Popularity refresh from the daily export (§1) |

## 8. How the numbers were measured

All figures dated 2026-08-16, against the live TMDB API and the production mirror
(229,443 shows, 5 users, 563 distinct tracked shows).

- **Endpoint behaviour** — 36 production shows sampled in three tiers of 12 (tracked /
  `vote_count` 5–200 / `vote_count` 0), each fetched as
  `GET /tv/{id}?append_to_response=recommendations,similar`, recording result counts and
  `total_results` for both namespaces.
- **Target resolution** — 30 source shows yielding 502 distinct recommendation targets,
  checked against `catalog.show.tmdb_id` for existence, `deleted_upstream_at` and `adult`.
- **Season window cost** — `max(season_number)` per show over all `catalog.season` rows
  carrying a `tmdb_id`, bucketed at 0–8, 0–7 and 0–6.
- **Anticipated** — `/discover/tv?first_air_date.gte=…&sort_by=popularity.desc&page=1`
  against the equivalent SQL, plus vote-count coverage and ranks 21–45 for tail quality.
- **Trending** — one `/trending/tv/week` call, all 20 ids checked against the mirror.
- **Popularity staleness** — `tmdb_synced_at::date` histogram over `catalog.show`, and the
  field set of `tv_series_ids_08_15_2026.json.gz` (229,150 lines, 5,002,466 bytes).
- **Query cost** — `EXPLAIN (ANALYZE, BUFFERS)` of the §4 query on the production mirror.

Probe scripts for the first three belong in `tvbf-backend/scripts/`, on the precedent of
`probe_tmdb_append_limit.py` and `probe_tmdb_season_speculation.py`, so a future reader
re-runs rather than trusts.
