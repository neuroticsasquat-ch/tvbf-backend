# NEU-1146 — Retire the TV Maze orphan rows

**Ticket:** [NEU-1146](https://linear.app/neuroticsasquatch/issue/NEU-1146/backend-map-the-782k-orphan-episodes-onto-their-tmdb-twins-so-tv-maze)
**Repo:** `tvbf-backend` (with a `tvbf-frontend` half — see §9)
**Project:** TVBF: TMDB Migration · Milestone 5, Migration & cutover
**Status:** approved for implementation

This spec lives in-repo rather than in the umbrella `docs/` because the frontend
half is a separate ticket in a different repo that has to cite it by URL — the
test the frontend deletes names this ticket by ID.

---

## 1. What this is for

TMDB is to be the **only** source of show data. Anything that is not in TMDB does
not belong in the database. `catalog` does not yet satisfy that: 782,161 episode
rows, 18,341 season rows and 2 show rows still hold TV Maze titles, airdates and
numbering, copied in by NEU-1042 and never mapped. While they are served, CC BY-SA
4.0 attribution is a licence condition, which is why `tvbf-frontend` PR #179 kept a
trimmed TVmaze credit in the footer.

This ticket empties that set: every orphan row that has a TMDB counterpart is
re-pointed onto it, every orphan row that does not is deleted, and the credit comes
out.

**This reverses a standing decision and needs an ADR.** ADR-0008 sanctioned
`tmdb_id IS NULL` as the way to hold content TMDB lacks, and the project's stated
hard constraint is *"No user loses a tracked show, a watched episode, or a rating.
Period. This holds even where TMDB has no counterpart."* Both are superseded here:
sole-source wins, and the 95 watch records in §6 are the measured cost of that.
Write **ADR-0012** recording the reversal, superseding the relevant half of
ADR-0008 and citing this spec's measurements.

---

## 2. Measurements

All figures measured against **production, 2026-08-14**, read-only. They reproduce
the ticket's headline numbers exactly.

The probe SQL and its full output — including the 108-row loss list — are kept
outside version control at `<umbrella>/docs/migration-working/NEU-1146-report-*`.
That is a **hand-written approximation of the report described in §5, not the pass
itself**; it exists so the design's evidence outlives the session that produced it.
The artifact that gets committed is the one §5's `report` mode prints on the day.

### 2.1 The surface is three tables

| Table | Orphans (`tmdb_id IS NULL`) | Total |
| -- | --: | --: |
| `catalog.episode` | 782,161 (782,155 under a matched show) | 7,340,279 |
| `catalog.season` | 18,341 | 410,914 |
| `catalog.show` | 2 | 229,221 |

Every other `catalog` table is **100% TMDB-sourced** — `person` (1,093,176),
`show_cast` (3,103,573), `character` (2,042,341), `show_aka` (158,743),
`content_rating` (223,422), `genre`, `network`, `keyword`, `production_company`,
`watch_provider`, `video`, `episode_group`: zero orphans in all of them. The
credits backfill (NEU-1127) is what made that true.

`catalog.episode.vote_average` — surfaced by the API as `rating_average` — is
TMDB's value, written by `tmdb/upsert.py`. No TV Maze rating data survives. Only
the *frontend naming* is stale (§9).

### 2.2 User attachment is 0.024%

189 distinct orphan episodes carry user data: **191 watch rows, 1 rating, 9
activity events**, across 5 users. **781,972 orphan episodes are referenced by
nothing.** Both orphan shows carry one `user_show_watch` row each and zero episode
watches. No `user_show_rating` rows point at an orphan show.

### 2.3 Why the exact key failed — every orphan classified

| Cause | Episodes | Shows | User-touched |
| -- | --: | --: | --: |
| Season absent from TMDB entirely | 576,792 | 3,738 | 17 |
| Number past the end of TMDB's season | 124,242 | 5,954 | 17 |
| Gap inside a season TMDB covers | 30,472 | 959 | 0 |
| Show matched but zero episodes ingested | 27,866 | 463 | 0 |
| Synthetic special (negative number, NEU-1042) | 21,012 | 5,055 | 155 |
| Ambiguous — one twin, 2+ copies | 891 | 91 | 0 |
| Exact `(season, number)` twin, 1:1 | 880 | 78 | 0 |
| Show unmatched | 6 | 1 | 0 |
| **Total** | **782,161** | | **189** |

Three genuinely different things are tangled here, and the distinction drives the
design:

* **Same episode, different address.** Lost `s1e-1 "The Journey"` → TMDB
  `s0e1 "The Journey"`, same date. NEU-1042 numbered specials negative within
  their original season; TMDB parks them in season 0. Pure addressing.
* **Same episode, different metadata.** Lost's *Missing Pieces*: our
  `"Missing Pieces 13: So It Begins"` against TMDB's `"Missing Pieces (13): So It
  Begins"` — punctuation, which the fold removes — with a uniform **7-day** date
  shift across all 8 rows.
* **No such episode.** Friends `s4e24 "The One With Ross's Wedding: Part II"`.
  TMDB counts that two-parter as **one** episode. This is not content TMDB is
  missing; it is the same content counted differently.

TV Maze and TMDB disagree about **what an episode is**, not about what aired.

### 2.4 The date is noise; uniqueness is the signal

Every user-touched orphan carries both a title and an air date (189 of 189). The
first design instinct — require folded title **and** exact air date — was tested
and rejected on evidence.

Among user-touched orphans with a *unique* folded-title match in their show, the
date delta distributes: 33 at zero, then 4, 6, 7 (×8, the *Missing Pieces* block),
10, 12, 101 … 5,332. **All 34 of the non-zero-delta matches were inspected
individually and every one is correct.** SNL's `"The Best of John Belushi"` —
TV Maze holds the 1998 broadcast, TMDB the 2005 compilation release. 30 Rock's
`"Live Show (West Coast Version)"` vs `"Live Show: West Coast Version"`. Not one
false positive.

The two catalogues date compilations and webisodes differently. **Uniqueness of
the folded title within the show is the reliable key; the air date is not.**

Requiring the date costs real matches: full-population same-show matching yields
**105,484** with exact date against **117,264** without, and **34** user-touched
against **64**.

### 2.5 Full-population tier dispositions — the report, run 2026-08-14

The whole ruleset of §3, applied in order against production:

| Tier | Rule | Orphans |
| -- | -- | --: |
| 0 | Exact `(season, number)` 1:1 | **880** |
| 1 | Same show, folded title unique both sides | **116,923** |
| 2 | Cross-show, `(season − offset, episode_number)` | **4,161** |
| 2b | Title fallback where no consistent offset | **53** |
| 3 | **Delete** | **660,138** |
| | Total | 782,155 |

Link resolution: 134 orphan shows had link candidates, **130 accepted**, 4 dropped
for carrying more than one candidate show. All 130 yielded a consistent offset.

Why rows reach tier 3:

| Reason | Orphans |
| -- | --: |
| No ingested episode with that title in the show | 593,120 |
| Ambiguous — 2+ orphans share the title | 62,395 |
| Ambiguous — 2+ ingested episodes share the title | 5,270 |
| Blank title after folding | 801 |

The offset key strictly dominates title+airdate: re-running the whole report under
each rule gives tier 2 **4,161 against 2,766**, and tier 3 correspondingly 660,138
against 661,586. The title fallback (2b) earns only 53 rows and **no user-touched
ones** — it exists for the Cunk-shaped case rather than for volume.

Tier 0's 880 rows accrued from catalog deltas since NEU-1126 ran on 2026-08-12 —
the same "re-run after any later ingest or delta" property CLAUDE.md records for
`season_dedupe`, and why tier 0 exists here rather than being assumed spent. Tier 1
measured in isolation finds 117,264; the 341-row difference is its overlap with
tier 0, correctly attributed once by the ordering.

### 2.5a Tier 2 makes 130 show links, and 129 of them are risk-free

The report's link list is longer than the ticket's single named case: 130 links
covering 4,161 episodes — *The Wonderful World of Disney* (356), *Whose Line Is It
Anyway?* (201), *Desus & Mero* (175), down a long tail that includes genuinely
generic names (`Big Brother`, `Family Feud`, `Idol`, `Paris`, `Eden`, `Elvis`,
`Extra`), where a same-name sibling is not evidence of the same show.

**This is benign, and the reason matters for §5's review burden.** For an orphan
carrying no user rows, tier 2 and tier 3 produce the *identical* end state — the
orphan row is deleted either way. Tier 2 only changes an outcome when user rows
ride along with the re-point. **129 of the 130 links carry zero user-touched rows.**
The only one that does is Will & Grace `549` → `1064267` (17), verified
episode-by-episode in §2.6.

So a wrong link among the other 129 costs nothing, and the review gate in §5 is
targeted rather than exhaustive: **only links with `user_touched > 0` need
scrutiny.** That property must hold for the pass as built — if a future change
makes tier 2 do anything beyond moving user rows and deleting the copy, this
reasoning lapses and every link needs review again.

### 2.6 Cross-show: the split-series case

Will & Grace is the ticket's named case and the measurement confirms it. The
revival is **already in the catalog** as show `1064267` / `tmdb_id 74321`; the
original is `549`. 57 orphan rows under `549`.

Folded title + exact airdate pairs **48**, covering 16 of 17 user-touched. The
**season offset is a constant 8** — seasons 9/10/11 against the revival's 1/2/3 —
and within it the episode numbers agree exactly (16/16, 17/17, 18/19). Pairing on
`(season − 8, episode_number)` instead of on the title pairs **52 of 57 and all 17
user-touched**. The five left over are synthetic specials with negative numbers,
none of them touched. **Will & Grace therefore loses nothing.**

The candidate set was bounded to ingested shows whose *folded show name* equals the
orphan's. That bound is loose on its own — "Lost" has 4 same-named ingested
siblings, "Friends" 7, and 1,937 orphan-bearing shows have at least one — but at
episode grain the title+date conjunction self-filters completely: run across all
125 tier-1 misses, it returned **16 matches, all Will & Grace, and zero false
positives anywhere else**.

### 2.7 The two orphan shows both have counterparts

| Orphan | Counterpart | Evidence |
| -- | -- | -- |
| `63900` "Cunk on Earth" | `1067768` "Cunk on…" **season 2** (`tmdb_id 79063`) | all 5 regular episodes title-match uniquely; dates differ (TV Maze recorded the Netflix drop date for all five, TMDB the weekly BBC dates) |
| `87519` "Discretion" | `1202502` (`tmdb_id 300966`) | exactly one same-folded-name ingested sibling; orphan has 0 episodes so there is no episode evidence |

TMDB models "Cunk on Earth" as a **season of an anthology**, not its own series.
Neither tracked show may be dropped — the counterpart is in the same table.

---

## 3. The matcher

Four tiers, applied in order, each stopping at the first hit. **Every tier requires
1:1 uniqueness on both sides and resolves any ambiguity to unmatched.** No tier
matches on air date alone or title alone across shows.

**Tier 0 — exact key, same show.** `(show_id, season_number, episode_number)` with
exactly one ingested and one copied row. NEU-1126's existing `_CANDIDATES`
predicate, unchanged.

**Tier 1 — unique title, same show.** Folded title (via `sql_fold.folded`, never a
Python-side fold) matches, and that folded title belongs to exactly one orphan and
exactly one ingested episode within the show. Blank folded titles never match, per
`folded_equal`'s existing rule. **Air date is not consulted** — §2.4.

**Tier 2 — show link, then the exact key *translated across the link*.** Three
steps, and the third is the one that matters:

1. **Establish the link.** An orphan show links to exactly one ingested show when
   either its episodes' folded-title + exact-airdate matches point at exactly one
   ingested show, or it has exactly one same-folded-name ingested sibling. More
   than one candidate, or none, means no link.
2. **Derive the season offset** from the pairs that established the link — the
   constant `orphan_season − twin_season`. Require it to be consistent across those
   pairs; an inconsistent offset means no offset, and step 3 is skipped.
3. **Pair on `(season_number − offset, episode_number)`**, 1:1 on both sides. This
   is tier 0's exact key with a constant translation, and it does not consult the
   title at all.

**Do not require the title in step 3.** Will & Grace `s9e1` is TV Maze's
`"Eleven Years Later"` and TMDB's `"11 Years Later"` — the fold reconciles
punctuation and case but not a spelled-out numeral against a digit, so a
title-gated rule drops the revival's premiere while placing all 16 episodes around
it. Measured: title+airdate pairs **48 of 57** and 16 of 17 user-touched; the
offset key pairs **52 of 57 and 17 of 17**. The five it leaves are synthetic
specials, none of them touched.

Where no consistent offset exists — a whole-show link whose counterpart is a season
of an anthology, the Cunk case — fall back to folded title alone within the linked
pair. Uniqueness on both sides throughout.

**Tier 3 — no counterpart. Delete.**

### 3.1 What the matcher must not do

* No title-only matching **across** shows — `"Rise of the Machines"` is an episode
  title in 12 different shows.
* No air-date-only matching at any grain.
* No resolution of ambiguity by primary key, in either direction. Two orphans
  sharing a title, or two ingested episodes sharing one, are both refused —
  NEU-1126's rule and its reasoning carry over verbatim.
* No `unicodedata` folding. `sql_fold.folded` is the one definition.

---

## 4. The pass

New module `src/tvbf/tmdb/orphan_retire.py` plus CLI
`src/tvbf/jobs/orphan_retire.py`, following `episode_repoint`'s shape exactly:
`report` writes JSON to stdout and nothing else (logs to stderr), `retire` does the
work, the process *is* the run so the exit code is the result, `--limit N` for a
smoke run, batched per transaction, idempotent, resumable by re-run from the start.

Taskfile targets `retire:orphans` and `retire:orphans:report`, both `silent:` with
`-T`, for the reason every other report target here is.

It **refuses to run before the full ingest** — re-use `episode_repoint`'s
`IngestNotRun` and `MIN_INGESTED_SHOWS`, which live there since NEU-1051.

### 4.1 Re-use, don't duplicate

The ticket is explicit and it is right: this is NEU-1126's shape. `episode_repoint`
already moves `user_episode_watch`, `user_episode_rating` and `activity_event`,
already knows the three uniqueness constraints those tables carry, and already
re-asserts its guards on the `DELETE`. Extract the write machinery rather than
authoring a second copy of it. The `_STILL_REFERENCED` / `_DELETE` pairing and the
`(episode, user)` unit of refusal are the parts worth keeping intact.

### 4.2 Collisions resolve by dropping the redundant row

This is the one behavioural reversal from NEU-1126, and it is deliberate.

NEU-1126 kept a copy whose user rows could not move (`blocked_by_collision`,
zero in production at the time) rather than merge two records into one. Here the
opposite is correct: when a user holds rows on **both** the orphan and its twin,
the twin's row already records that viewing, so the orphan's row is **redundant and
is deleted**. Nothing is lost — one viewing keeps one row.

This is exactly the two-parter case. A user who watched Friends `s4e23` and
`s4e24 "Part II"` has two rows for what TMDB models as one episode; after the pass
they have one. That is a re-count under TMDB's episode model, not a loss, and the
reconciliation harness must be told to expect it (§7).

Applies identically to `user_episode_watch`, `user_episode_rating` and
`activity_event` (whose `uq_activity_event` is `NULLS NOT DISTINCT` on
`season_number` — compare with `IS NOT DISTINCT FROM`, as `episode_repoint` does).

### 4.3 Episodes that move to a show the user does not track

When a re-point moves a user's episode row into a **different** show — tier 2 —
the pass **inserts a `user_show_watch` row** for that user and the destination
show, if one does not already exist. Without it the history is intact by row count
and invisible in the product: Watch Next, progress and the show page all key off
the tracked show. Will & Grace is the live case — 16 watches move to `1064267`,
which nobody tracks.

Same rule for a whole-show link (tier 2 by show): `user_show_watch` and
`user_show_rating` on the orphan show move to the linked show — Cunk on Earth
`63900` → `1067768`, Discretion `87519` → `1202502`.

**This is the only place the pass creates an `app` row rather than moving one.**
It must be counted and reported separately, and it is an expected reconciliation
*gain* (§7).

### 4.4 Order of deletion

Episodes, then seasons, then shows — an orphan season is deletable once it holds no
episodes, an orphan show once it holds no seasons. Two orphan seasons hold
*ingested* episodes and must be handled by the existing `catalog/seasons.py` read
rule rather than deleted out from under them.

Note the operational warning in `docs/migration/README.md`: bulk episode deletion
needs `ix_show_last_episode_to_air_id` and `ix_show_next_episode_to_air_id`
(migration `f85a608ef19e`). They exist. Do not remove them.

### 4.5 Re-runnability

There is no watermark — a row leaves the work list by being re-pointed or deleted.
Like `season_dedupe`, **re-run this after any later ingest or delta**: a delta can
add an ingested episode that gives an existing orphan a twin. Tier 0's 880 rows are
that mechanism already observed.

---

## 5. The report

`report` mode, runnable against production before anything is spent, writing JSON
to stdout. It must break the orphans down **by cause** using §2.3's categories and
**by tier disposition**, with:

* per-tier match counts, and the same counts restricted to user-touched rows;
* rejection counts split by *reason* — ambiguous on the orphan side, ambiguous on
  the ingested side, blank title, no counterpart — never folded into one "unmatched"
  bucket;
* every show link tier 2 would make, listed explicitly, with the evidence that
  produced it **and its user-touched count**. 130 links, and per §2.5a only those
  with `user_touched > 0` need review — but the count has to be on the row for that
  triage to be possible;
* the exact set of user rows that would be **deleted rather than moved**, per user,
  per show, with episode title and air date — the §6 loss list, reviewable before
  it happens — **split into two dispositions**: rows the user already holds on the
  surviving twin (a de-duplication, no loss) and rows with no counterpart at all (a
  genuine loss). Folding those together would report ~109 losses where roughly 92
  are real;
* the count of `user_show_watch` rows that would be **created**.

Run it and read it before running the pass.

---

## 6. The accepted loss

Measured 2026-08-14. Of the 189 user-touched orphans: **64 move via tier 1, 17 via
tier 2, 108 are deleted.** Tier 0 catches none of them.

At row grain: **82 watch rows move**, **109 watch rows and 1 rating row are
deleted**. (The report as first run showed 16/109/110/96, before tier 2 adopted the
season-offset key of §3; the difference is Will & Grace `s9e1`.) The deleted watch
rows split:

* **14 are de-duplications** — the user already holds a watch on the ingested
  episode that absorbed this one, so one viewing keeps one row and nothing is lost
  (§4.2). Friends `s4e24 Part II`, `s5e24`, `s7e24`, `s8e24`, `s9e24`, `s10e18`;
  Lost `s1e25 "Exodus (3)"`, `s4e14`, `s6e18 "The End (2)"`; Parks `s6e21`,
  `s7e13 "One Last Ride (2)"`; Brooklyn Nine-Nine `s8e10 "The Last Day (2)"`;
  The Hook Up Plan `s2e7`.
* **95 are a genuine loss** — content TMDB does not model as an episode of that
  series. Overwhelmingly SNL (≈60 rows: `The Best of Will Ferrell, Volume 1`,
  `SNL 40th Anniversary`, `Presidential Bash 2008`, `SNL50: The Red Carpet`), plus
  webisode runs (Parks' `April & Andy's Road Trip`, Brooklyn Nine-Nine's
  `Hitchcock and Scully Webisode`), films filed as episodes (`El Camino: A Breaking
  Bad Movie`, `Sex and the City: The Movie`, `Friends: The Reunion`,
  `Unbreakable Kimmy Schmidt: Kimmy Vs. The Reverend`), and one-off specials
  (`30 Rock: A One-Time Special`, `A Farewell to Ozark`, The Bear `Gary` ×2 users).

One entry deserves naming because it is *not* the pattern:

* **Friends `s6e25 "The One With the Proposal, Part 2"`** classifies as a loss
  while its `Part 1` twin classifies as a de-duplication, because the probe checks
  the adjacent ingested number and TMDB merged this pair the other way round. It is
  almost certainly a de-duplication too. **The pass's own classifier must not use
  the adjacent-number heuristic** — it should ask directly whether the user holds a
  watch on the ingested episode this orphan's season/number range collapses into.

**The 96 are a real, accepted loss of watch records** — the measured cost of
sole-sourcing from TMDB, recoverable by hand from `app.watch_archive`, which holds
a human-readable snapshot of all 9,359 watches, has no foreign keys, and survives
everything.

These figures move as deltas land. **Re-run the report immediately before the pass
and diff it against this section**; the loss list it prints on the day is the
artifact to review and commit, not this one.

---

## 7. Reconciliation

`task reconcile:verify` is the acceptance test, and **it will not come back clean,
by design.** Three expected discrepancy classes, all of which must be enumerated in
advance and confirmed line-by-line against the report:

1. **Losses** — the 95 watch records of §6. Every one must appear on the report's
   loss list. **A `LOST` line not on that list is a stop.**
2. **Losses that are de-duplications** — the 14 two-parter halves of §4.2 and any
   further collision drops. Same rule: on the list, or stop.
3. **Gains** — the `user_show_watch` rows of §4.3, plus whatever ordinary app use
   has added since the baseline (NEU-1125 recorded eight such gains).

Capture a fresh baseline immediately before the run, and diff the post-run state
against it and against the report. Record the outcome in the run log.

---

## 8. Acceptance criteria

1. `report` runs against production, breaks all orphans down by cause and by tier
   disposition, and lists every proposed show link and every user row that would be
   deleted rather than moved. **Run and read before the pass.**
2. Every orphan with a TMDB counterpart under tiers 0–2 is re-pointed; user rows
   move with it; the copy is deleted.
3. Ambiguity resolves to unmatched at every tier and appears on the report with its
   reason. No air-date-only or title-only-across-shows matching.
4. Orphans with no counterpart are deleted, with their user rows. The loss matches
   the report's loss list **exactly** — no unlisted `LOST` line. Baseline from the
   2026-08-14 run: 82 watch rows moved, 109 deleted (14 de-duplications, 95 genuine
   losses), 1 rating deleted, 1 `user_show_watch` created.
5. Episodes moving to a show the user does not track cause a `user_show_watch` row
   to be written; whole-show links move `user_show_watch` / `user_show_rating`.
   Both counted and reported.
6. Will & Grace `549`→`1064267`, Cunk on Earth `63900`→`1067768`, Discretion
   `87519`→`1202502` all resolve automatically. No hand-linking, no human queue.
   **All 17 Will & Grace user-touched rows move** — a run that rescues only 16 has
   fallen back to title matching and dropped `s9e1 "Eleven Years Later"`.
7. After the pass: `catalog.episode`, `catalog.season` and `catalog.show` hold
   **zero** rows with `tmdb_id IS NULL`. Verified by query, and the pass's own
   report says so.
8. Reconciliation run either side, discrepancies confirmed against §7's three
   classes, outcome recorded in `docs/migration/README.md`'s run log.
9. ADR-0012 written (§1).
10. The frontend half (§9) lands only after criterion 7 holds in production.

---

## 9. The frontend half

A separate ticket in `tvbf-frontend`, citing this spec, landing **after** the
backend pass has run in production and criterion 7 holds:

* Remove the TVmaze CC BY-SA sentence from the footer in
  `src/components/AppShell.tsx` (currently lines ~264–286). Keep the TMDB
  attribution sentence **verbatim** — NEU-1049 requires that wording and it must
  not be reworded while editing around it.
* Delete the test that pins the credit,
  `src/components/AppShell.test.tsx` → *"keeps the TVmaze CC BY-SA credit while
  TV Maze-derived data is still served"*, and the comment above it naming this
  ticket.
* Cosmetic cleanup, since the point is that no TV Maze trace remains: rename
  `tvmazeToFiveStar` in `src/lib/rating.ts` (and its callers in `ShowCard.tsx`,
  `ShowDetailPage.tsx`, `EpisodePage.tsx`, `rating.test.ts`), and replace the
  `"TV Maze average"` `RatingBadge` tooltip on `ShowDetailPage.tsx:99` and
  `EpisodePage.tsx:156` — the underlying value is TMDB's `vote_average` (§2.1).

**Out of scope, deliberately:** the `tvmaze_updated` sort key and the
`ShowDetail.tvmaze_updated` response field. CLAUDE.md records these as intentional
legacy aliases whose renaming is a coordinated two-repo contract change; they are
field *names*, not TV Maze data, and no licence condition attaches to them.

---

## 10. Documentation to update

* **`tvbf-backend/docs/adr/0012-*.md`** — the reversal (§1).
* **`docs/migration/README.md`** — a run-log row for this pass, and a runbook
  section covering the report-first ordering, the expected reconciliation
  discrepancies, and the fact that this is **not reversible**: the pre-drop
  `tvmaze` dump is the only source for the deleted rows, and it cannot restore
  `app` rows at all.
* **`tvbf-backend/.claude/CLAUDE.md`** — the DB-topology section (no more
  `tmdb_id IS NULL` rows in the spine), the migration-operations list (the new
  task), and the non-obvious-patterns entries that currently describe orphan rows
  as permanent residue: the copied-special negative-numbering entry, the
  `catalog/seasons.py` read-rule entry (18,339 kept seasons), and the
  `episode_repoint` entry (189 kept episodes). All three describe a state this
  ticket ends.

---

## 11. Out of scope

* **NEU-1145** (Apple TV+ airdates a day early). Related — it is why an air-date
  matcher must not assume exact equality — but §2.4 removes the date from tiers 0
  and 1 entirely, and tier 2's use of it is bounded by an already-narrow candidate
  set. No dependency in either direction.
* Re-deriving `catalog.show.id` away from preserved TV Maze ids. Ids are not
  content; ADR-0008's surrogate-key decision stands.
* Any change to `tvmaze_updated` as an API field or sort key (§9).
