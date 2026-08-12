# Migration artifacts

Fixed home for the TMDB migration's reconciliation baseline (NEU-1030) and the
one-off repair scripts the migration's production runs turn out to need. The
location is pinned here rather than improvised at cutover, because milestone 5's
go/no-go re-runs the same harness against the same file — and because a
procedure that only exists in somebody's terminal history is a procedure that
will not survive to the day it is needed.

## Production run log

**Merging a pass is not running it.** Every job below is a one-shot operation whose
value comes from being executed against production, several have ordering windows
that close, and nothing about a merged PR records whether the run happened. This
table is that record — update the row in the same PR that runs the pass, or in a
follow-up if the run comes later.

The cost of not having had this: NEU-1045 merged on 2026-08-11, a day *after* the
full catalog ingest started, so its window had already shut. Nobody noticed until
NEU-1066 measured the episode grain and found all 7,137 watched episodes still
pointing at unmapped rows.

| Pass | Task | Ordering | Run in prod |
| -- | -- | -- | -- |
| Watch archive (NEU-1029) | `task archive:watches` | before anything else | ✅ 2026-08-09 — 9,359 rows |
| Reconciliation baseline (NEU-1030) | `task reconcile:capture` | before cutover | ✅ 2026-08-09, **re-captured 2026-08-11** after the prune — 5 users, 621 tracked shows, 8,569 episode watches, 97 show ratings, 78 episode ratings, 802 activity events |
| Catalog copy (NEU-1042) | `task copy:catalog` | before enrichment | ✅ 2026-08-09 — 89,025 shows |
| TMDB id enrichment (NEU-1043) | `task enrich:tmdb-ids` | after copy, **before ingest** | ✅ 2026-08-10 — 62,882 matched, 26,143 unmatched, 107 collisions |
| Collision remediation (NEU-1065) | `neu-1043-collision-remediation.sql` | after enrichment | ✅ 2026-08-10 — 18 rows |
| Human queue (NEU-1044) | `task queue:confirm` / `queue:reject` | after enrichment, **before ingest** | ⚠️ partial — 4 guesses confirmed 2026-08-10; 2 user-touched rows still unresolved, and the window has closed (see NEU-1066) |
| Episode-grain mapping (NEU-1045) | `task map:episodes` | after enrichment, **before ingest** | ❌ **never run — window closed.** The ingest started 2026-08-10, this merged 2026-08-11. Running it now maps nothing: 1,909,367 rows collide and 760,254 have no TMDB counterpart. Needs a re-point pass instead. |
| Full catalog ingest (NEU-1034) | `task ingest:catalog` | after copy + enrichment | ✅ 2026-08-10 → 2026-08-11 — 228,723 shows |
| Season-grain dedupe (NEU-1119) | `task dedupe:seasons` | after ingest; re-run after any delta | ✅ 2026-08-11 — 122,350 deleted, 2,125,419 episodes re-pointed |
| Show-grain prune (NEU-1066) | `task prune:shows` | after ingest | ✅ 2026-08-11 — 26,141 shows deleted over 262 batches, taking 47,443 seasons and 840,169 episodes. `catalog.show` 255,010 → 228,869; 2 unmatched rows kept. Needed the `ix_show_*_episode_to_air_id` indexes first (see below) |
| User-touched remediation (NEU-1066) | `neu-1066-user-touched-remediation.sql` | **after NEU-1046**, then re-run the prune | ⬜ blocked — the FK still points at `tvmaze.show` |
| Pre-cutover go/no-go (NEU-1048) | `task gate:coverage` | **before NEU-1046**, while `tvmaze` still stands | ⬜ not yet run — writes nothing, so run it as often as it takes to reach a go |
| Episode-grain re-point (NEU-1126) | `task` TBD | **after NEU-1046**, before NEU-1047 | ⬜ not built — 2,690,633 copied episodes still duplicate the ingested ones, and all 7,137 watched-or-rated episodes point at the copies |

## Deleting episodes needs two indexes that did not exist

`catalog.show.last_episode_to_air_id` and `next_episode_to_air_id` are
`ON DELETE SET NULL` foreign keys into `catalog.episode`, and until 2026-08-11
neither had an index — so Postgres sequentially scanned all 255,010 shows for
**every episode deleted by a cascade**. The first `task prune:shows` attempt could
not finish one batch of 100; a single show with 10-40 episodes ran past 60
seconds. Migration `f85a608ef19e` adds them, and the same delete then took 4.6ms.

Worth remembering because the shape recurs: every *other* foreign key into the
catalog spine has a leading index by accident of sitting on a lookup path. A
column nothing reads by gets no index, and the cost only appears when something
deletes in bulk. If a future pass deletes catalog rows and stalls, audit the
inbound foreign keys for a missing leading index before anything else.

## `reconciliation-baseline.json`

Per-user, per-show counts of tracked shows, episode watches, show ratings,
episode ratings and activity events, plus totals. Produced by
`python -m tvbf.jobs.reconcile capture`.

Deterministic: sorted users, sorted shows, sorted keys, trailing newline. Two
captures of an unchanged database are byte-identical, so `git diff` on this file
is meaningful and a real change is never lost in reordering noise.

It holds user **ids**, never emails — it is committed. Names are resolved from
the live database only when a discrepancy is reported.

### Capturing the baseline (production)

The baseline that matters is production's, taken **before** any cutover work.
The artifact travels on stdout, because `docs/` is not in the production image
and a Coolify container is replaced on every deploy:

```bash
ssh tom@ssh.neuroticsasquat.ch \
  'docker exec <tvbf-backend-container> python -m tvbf.jobs.reconcile capture' \
  > docs/migration/reconciliation-baseline.json
```

Commit the result. Locally, `task reconcile:capture` writes the same file from
the dev database — useful for trying the harness out, **not** a substitute for
the production baseline.

### Verifying

```bash
# local
task reconcile:verify

# production, baseline on stdin
ssh tom@ssh.neuroticsasquat.ch \
  'docker exec -i <tvbf-backend-container> python -m tvbf.jobs.reconcile verify --baseline -' \
  < docs/migration/reconciliation-baseline.json
```

**Exit 0 means nothing moved; exit 1 means something did**, and every
discrepancy prints with the user and the show it belonged to. Gains fail as
loudly as losses — an unexpected gain during a cutover window means something
ran that should not have.

After cutover, add `--spine catalog` so the episode→show joins resolve against
the new schema. The show ids are unchanged by design (TV Maze ids are preserved
as `catalog.show.id`), which is what lets one baseline span both spines.

## `neu-1043-collision-remediation.sql`

**Executed against production 2026-08-10.** 107 collisions, 18 repairable: 18
guesses retracted, 18 exact matches stamped, and no row on either side carried
user data. NEU-1043 orders the mapping tiers correctly within a show but not
between shows, so a tier-3 title guess on a low-id row could take a `tmdb_id`
that a higher-id row matched exactly. NEU-1065 fixes the cause in code; this
script repaired the data.

Kept runnable rather than reduced to a record: another enrichment pass before
NEU-1065 lands would produce the same class of collision, and its inspection
queries are reusable against any run's log.

**Its input is the run's log, not the database.** The database records that a row
is unmatched, never that it lost a contest or to whom, so the collisions have to
be lifted out of the enrichment log first — step 1 in the file's header. Lose the
log and the only way back is another enrichment run, capturing the warnings.

It repairs only collisions that were *logged*. A tier-3 false positive that
contested nothing is invisible to both the log and the database, and is caught
only by reading `match_method = 'title_year'` rows by hand.

## The human matching queue (NEU-1044)

No artifact here — the queue is a query, run live, because a snapshot of it goes
stale the moment somebody resolves a row. `python -m tvbf.jobs.human_queue`, and
`src/tvbf/tmdb/human_queue.py` explains what it surfaces and why.

**Its output names users by email and does not belong in this directory.** That
is the deliberate opposite of `reconciliation-baseline.json` above, which holds
ids because it is committed; the queue names people because "who would lose
data" is what decides how hard to look at a row. Read it and discard it.

**Run it before the full TMDB ingest.** Once the ingest has inserted a row per
series, the series a queue row should map onto already holds that `tmdb_id`, and
`confirm` can only report the collision.

```bash
# every user-touched show without a verified mapping, plus TMDB candidates
task queue:list

# the same, no upstream calls and no credential — the fast "is it empty?" check
task queue:list -- --no-candidates

# production
ssh tom@ssh.neuroticsasquat.ch \
  'docker exec <tvbf-backend-container> python -m tvbf.jobs.human_queue list'
```

Then one command per row, each of which is a recorded verdict:

```bash
task queue:confirm -- <show_id> <tmdb_id>   # this show IS that TMDB series
task queue:reject  -- <show_id>             # TMDB has no counterpart; stays locally-authored
```

**Exit 0 means the verdict was written; exit 1 means it was refused** — an id
another row already holds, a row matched exactly (which the queue never
surfaced), or a second verdict over an existing one. `list` exits 0 either way:
it is a report, and *empty* is what the cutover gate reads out of it.

### The four production guesses confirmed on 2026-08-10

NEU-1044's ticket records four `title_year` matches checked by hand and found
correct — but the check lived only in the ticket, where the rows still read
`match_method = 'title_year'`, indistinguishable from an unreviewed guess.
Re-stamping them is what moves that verdict into the database:

```bash
task queue:confirm -- <show_id> 119955   # Dr. Brain
task queue:confirm -- <show_id> 225634   # Monsters: The Lyle and Erik Menendez Story
task queue:confirm -- <show_id> 241849   # You Are What You Eat: A Twin Experiment
task queue:confirm -- <show_id> 299737   # The Traitors Ireland Uncloaked
```

`confirm` with the id a row already holds re-stamps it rather than refusing, so
these are exactly the four commands and nothing else. Take each `<show_id>` from
`task queue:list` rather than from here — the catalog id is a local surrogate and
was never in the ticket.

*The Traitors Ireland Uncloaked* is the only one carrying watch history (12
episode watches), and confirming its show id is necessary but not sufficient:
those watches must also map at episode grain, which is NEU-1045's.

## Episode-grain mapping (NEU-1045)

No artifact here either — the pass writes to the database and the report is a
query, run live and read once, for the same reasons the queue above is.

**Run it after `task enrich:tmdb-ids` and before the full TMDB ingest.** The
episode upsert conflict-targets `tmdb_id`, and a copied episode row carries
none: run the ingest first and every matched show ends up holding each episode
twice — a TMDB row with a fresh id, and the copied row that
`app.user_episode_watch` actually points at. Stamping the copied rows first is
what makes the ingest update them in place instead.

```bash
# the pass: one request per matched show, resumable, safe to kill
task map:episodes
task map:episodes -- --limit 100     # smoke run first

# production
ssh tom@ssh.neuroticsasquat.ch \
  'docker exec <tvbf-backend-container> python -m tvbf.jobs.episode_map map'
```

Then read the residue. Unmatched episodes are the expected output rather than a
failure — a two-parter counted once upstream and twice here has no key to match
on — so the report surfaces only the ones somebody would lose something over:

```bash
task map:episodes:report
```

Four things in it are worth reading, in this order:

1. `unmirrored_watches` — watched episodes with **no `catalog.episode` row at
   all**, which every other number in the report is computed without. The TV
   Maze daily keeps adding episodes until cutover and every one added after
   `task copy:catalog` ran is watchable while having nothing to map, so this is
   expected to be non-empty on any day the copy has not just been re-run. The
   fix is operational: re-run `task copy:catalog`, then this. The CLI logs it as
   an error above the counts for the same reason.
2. `systematic_shows` — a matched show where **nothing** mapped. That is a claim
   about the show rather than its episodes, and usually means the `tmdb_id`
   NEU-1043 attached belongs to a different series, which `task queue:confirm`
   fixes. Check it against the run's log first: the flag is `0 of N mapped`, and
   a show whose fetch failed during the pass satisfies that exactly as well —
   the database cannot tell those apart, but the pass's per-show warnings can.
3. `unmatched_user_data` — every unmapped episode carrying a watch or a rating,
   worst first. Each row keeps its TV Maze data and its watch record whatever
   happens; the entry is there so a systematic pattern on a popular show is
   visible rather than inferred. `synthetic: true` marks a row the copy invented
   a negative number for — a null-numbered TV Maze special, permanently
   unmappable and not a mismatch to investigate.
4. `totals.watched_episodes_unmapped` — the number the cutover gate reads.

The report only means anything **after** the pass: before one, every matched
show has zero mapped episodes and reads as systematic.

Two rows are genuinely unmapped and need a decision either way: *Cunk on Earth*
(several *Cunk* entries, so the rule refused to guess) and *Discretion* (no
premiere date, excluded from tier 3 by design). Neither carries watch history.

## Season-grain deduplication (NEU-1119)

The copy left every `catalog.season` row with `tmdb_id IS NULL`, and nothing ever
mapped the season grain — so once the full ingest ran, every matched show ended
up carrying two rows for each season: the copied one under its preserved TV Maze
id, and the ingested one under a fresh surrogate. NEU-1119 chose **delete** over
map because the ingest had already run (228,841 shows synced), which closes the
mapping window `uq_season_tmdb_id` guards.

**Run `report` first.** It writes nothing and says exactly what the pass would do:

```bash
task dedupe:seasons:report

# production
ssh tom@ssh.neuroticsasquat.ch \
  'docker exec <tvbf-backend-container> python -m tvbf.jobs.season_dedupe report'
```

Measured against production on 2026-08-11, of 188,134 copied seasons:

| | rows | |
| -- | -- | -- |
| `deletable_duplicates` | 122,350 | deleted |
| `kept_under_unmatched_show` | 47,445 | the only season data those shows have |
| `kept_no_counterpart` | 18,339 | TMDB has no season of that number |
| `ambiguous` | 0 | two ingested rows for one number — refused, not guessed |
| `episodes_to_repoint` | 2,125,419 | 7,120 of them carrying watch or rating history |

Then the pass. It re-points the episodes onto the surviving season and deletes
the copy, in one transaction per 500 seasons — safe to kill, and resumable
because a row leaves the work list by being deleted:

```bash
task dedupe:seasons -- --limit 100    # smoke run first
task dedupe:seasons
```

Read the report again afterwards. `deletable_duplicates` should be `0` — which
says the pass has nothing left to do, **not** that the season grain is clean.
`still_doubled` is what scores that, and it does not reach zero (below).

Four things about it are worth knowing before running it in production:

1. **`episodes_to_repoint` is not zero, and the ticket said it would be.**
   NEU-1119 assumed `upsert_episodes` had already moved these episodes onto the
   ingested season. It moves only the episodes it *writes*, and a copied episode
   with no `tmdb_id` is not one — so a bare `DELETE` would trip `ON DELETE SET
   NULL` on 2.1 million rows, 7,120 of them watched. The pass re-points first.
2. **`still_doubled` stays non-empty, and that is correct.** It lists every
   `(show, season number)` that will carry more than one row *after* the pass —
   the residue of "no show carries two `catalog.season` rows for one season",
   which this pass cannot fully reach. Three shapes, told apart by two fields:
   `show_matched: false` is TV Maze's own duplicate numbering under a
   locally-authored show (**9** pairs in production), where "a season under a
   locally-authored show is untouched" wins; `show_matched: true` with
   `ingested_rows: 0` is the same duplicate under a matched show on a number TMDB
   has no season for, so neither row has a counterpart to defer to (**33** pairs);
   and `ingested_rows` above 1 is two rows the *ingest* wrote for one number,
   which is the ambiguity the pass refuses rather than guesses at (**0**). Forty-two
   pairs in total, measured 2026-08-11.
3. **`task copy:catalog` puts the rows back, but not the parentage.** Its
   anti-join verification demands a catalog row for every `tvmaze.season`, so
   re-running it re-inserts each deleted row under its original id — and until it
   is re-run, `verify_copy` reports `catalog.season` short. It does **not**
   restore `catalog.episode.season_id`: `_COPY_EPISODES` skips rows already
   present, so a bare re-copy hands back the seasons with no episodes attached. A
   full revert is two statements, the second being:

   ```sql
   UPDATE catalog.episode e
      SET season_id = te.season_id
     FROM tvmaze.episode te
    WHERE te.id = e.id AND te.season_id IS NOT NULL;
   ```

   `tvmaze.episode.season_id` holds every original pointer for as long as that
   schema stands (NEU-1051 has not run), which is what makes the work reversible
   in full rather than in part.
4. **The pass is safe to kill.** It commits per 500 seasons, so an interrupted
   run keeps everything earlier batches did and the next run picks up the rest.

Re-run the pass after any later ingest or catalog delta: a delta that adds a
season to a matched show on a number a copied row still holds is a fresh
duplicate, and there is no watermark to make that a one-shot.

## Show-grain prune (NEU-1066)

The show grain's version of the same problem, with the opposite outcome for
matched rows. `catalog.show` upserts conflict-target `tmdb_id` (ADR-0008) and
Postgres treats NULLs as distinct in a unique index, so the full ingest split the
copied population in two: a row NEU-1043 had mapped conflicted and received the
TMDB payload *on the same row* — preserved id, no duplicate — while a row with
`tmdb_id IS NULL` could not conflict with anything, and TMDB's series was inserted
beside it under a fresh surrogate.

So `catalog` holds two rows for one show wherever matching failed and TMDB has the
series anyway: id 10158 with TV Maze's "ITV News at Ten" and id 1003587 with
TMDB's, each carrying its own disjoint seasons and episodes.

### Why the rule is "unmatched and untouched", not "duplicate"

The ticket proposed hiding duplicates from discovery, on the grounds that an
unmatched row is ambiguous between "TMDB has it and we failed to match" and "TMDB
does not have it". The ingest dissolves that: TMDB's whole catalog is local now,
so the question is answerable in SQL. Measured against production 2026-08-11, of
26,143 unmatched rows only 6,464 share a folded title with an ingested row and
3,337 also agree on first-air year — three quarters duplicate nothing at all.

That is what makes the simpler rule right rather than blunt. **The catalog is
TMDB, plus the shows users have history on.** Locally-authored rows exist to hold
the no-loss guarantee, not to preserve the breadth of the source being retired.

Stated rather than discovered: the pass drops 26,141 shows including 4,898 Russian
and 2,326 Chinese entries — the long tail the project spec flagged as unproven.
2,406 of them have no episodes at all. `task copy:catalog` restores every one of
them under its original id while `tvmaze` stands.

### Running it

```bash
task prune:shows:report          # writes nothing; run this first

# production
ssh tom@ssh.neuroticsasquat.ch \
  'docker exec <tvbf-backend-container> python -m tvbf.jobs.show_prune report'

task prune:shows -- --limit 100  # smoke run
task prune:shows
```

Expected against production:

| | rows | |
| -- | -- | -- |
| `deletable` | 26,141 | deleted, taking 47,443 seasons and 840,169 episodes |
| `deletable_with_title_twin` | 6,464 | duplicates the matcher missed |
| `deletable_without_title_twin` | 19,679 | no ingested row shares the title — breadth from the retired source |
| `kept_user_touched` | 2 | enumerated below — the accepted exceptions |
| `kept_human_verdict` | 0 | `match_method='human'` with no `tmdb_id` is a person's ruling |
| `kept_not_copied` | 0 | a row authored after the migration is not a copy |

The pass **refuses to run before the full ingest** — under 150,000 shows carrying
`tmdb_synced_at` it raises `IngestNotRun` rather than treating every copied row as
unmatched and taking all 89,025.

### The two shows it keeps, and why neither is a locally-authored row

Both were checked against the live TMDB API on 2026-08-11, and **neither is absent
from TMDB** — which is the general rule worth carrying: "unmatched" almost always
means a grain mismatch or a failed lookup, not a missing series.

| Show | catalog id | What TMDB actually has |
| -- | -- | -- |
| Discretion | 87519 | A plain duplicate — TMDB 300966, already ingested as id 1202502 |
| Cunk on Earth | 63900 | **Season 2 of "Cunk on…"** (TMDB 79063, ingested as 1067768, seasons *Britain* and *Earth*). `/find` by its tvdb id returns nothing and tier 3's search returns four results with no exact title match, so both tiers correctly declined |

Each is one `app.user_show_watch` row and nothing else — no episode watches, no
ratings, no activity events, same user. `queue:confirm` cannot resolve them
post-ingest (`uq_show_tmdb_id` refuses, since the ingested row already holds the
id), so the fix is `neu-1066-user-touched-remediation.sql`: re-point the two My
Shows rows, then re-run `task prune:shows` to take the copies. **That file cannot
run until NEU-1046 has repointed the foreign keys** — `app.user_show_watch.show_id`
still references `tvmaze.show.id`, and both destinations are catalog surrogates
above TV Maze's highest id, so the write is refused today. Note that the Cunk
row changes what that user sees on their list, from "Cunk on Earth" to "Cunk on…".

`still_doubled` in the report is the scoreboard for *"no show appears twice"*, and
its limit is worth stating: it matches on **exact folded title equality**, so it
sees "Discretion" and will be empty after the remediation — but it cannot see a
grain mismatch. "Cunk on Earth" against TMDB's "Cunk on…" is a genuine duplicate
that no title comparison will ever pair, which is why the two kept rows are
enumerated above by hand rather than left for the query to find. Read an empty
`still_doubled` as "no title-identical duplicate remains", not as "the show grain
is clean".

## Pre-cutover go/no-go (NEU-1048)

The last check before the window opens, and the only one that runs while there
is still nothing to undo. `task gate:coverage` writes one JSON artifact to
stdout and **exits 0 on a go, 1 on a no-go, 2 if it could not run** — the third
kept separate because a crashed gate must never be filed as a considered
verdict.

It writes nothing. Reading is the whole job, so run it as often as it takes to
get to a go.

### The no-go criteria

**Fixed before the report was first run**, which is the point of writing them
down here rather than deciding them once the numbers are in. Each is a way the
cutover breaks user data; any one failing is a no-go.

| # | Criterion | Why it is a no-go |
| -- | -- | -- |
| 1 | `fk_targets_resolve` | Every id in the five columns NEU-1046 repoints resolves against `catalog`. This is the precondition that ticket's `ALTER TABLE` enforces anyway — asked here while a dangling id is still a report line rather than a migration that failed halfway through the window. `import_ne.show_resolution` is checked only when the schema exists, and its absence is reported as such: *"no dangling rows"* and *"did not look"* are the two answers a gate must never conflate |
| 2 | `user_touched_shows_present` | Every show a user has touched has a `catalog.show` row. Distinct from criterion 1 because `app.activity_event` is polymorphic with **no foreign key at all** — it neither blocks nor cascades, it silently orphans, so no `ALTER TABLE` would ever catch it |
| 3 | `user_touched_shows_resolved` | Every show a user has touched has reached a mapping nobody still has to check — an exact tier (`tvdb_id` / `imdb_id`), a `match_method = 'human'` verdict, or the enumerated exception list below. **A bare `title_year` guess does not count**, even carrying a `tmdb_id`: this is `human_queue`'s own predicate, spelled the same way, because NEU-1044's criterion is that *"tier-3 matches on user-touched shows are surfaced for review, not trusted silently"* and a false positive at show grain attaches a user's watch history to the wrong show. Confirming a guess re-stamps it `'human'`, which is what clears it |
| 4 | `ingest_present` | At least 150,000 shows carry `tmdb_synced_at`. Same floor and same device as `show_prune`'s `IngestNotRun` guard and the tombstone's plausibility check — under it, every measurement in the artifact is about a half-built catalog |

### The two accepted exceptions

`ACCEPTED_UNRESOLVED` exempts catalog ids **87519** (*Discretion*) and **63900**
(*Cunk on Earth*) from criterion 3. Both are enumerated above under the
show-grain prune, both are duplicates rather than locally-authored rows, and
both are resolved by `neu-1066-user-touched-remediation.sql` — which cannot run
until NEU-1046 has repointed the foreign keys, i.e. until *after* this gate.
Without the exemption a known, sequenced remediation would read as a fresh
discovery and the gate could never pass.

The list is an exemption, never an assertion: a row that stops needing exempting
simply stops appearing, and **a user-touched row that is not on it is a no-go.**
That is where the teeth are.

### Coverage is measured, not gated

The language and era breakdown answers the one risk ADR-0007 accepted without
measuring — *"228,611 > 88,971 is a count, not a guarantee TMDB holds our 4,536
Russian and 3,243 Chinese entries"* — and the answer is now known to be **no**.
NEU-1066's prune deliberately dropped 26,141 unmatched copied shows, 4,898
Russian and 2,326 Chinese among them, on the rule that *the catalog is TMDB plus
the shows users have history on*. So a breadth threshold here would fail by
construction against a decision the project has already taken and merged, and
the project spec says as much in one line: *"a catalog comparison … catches a
long-tail regression before the window. It is a safety check, not a decision
gate — the decision is made."*

Each bucket splits its TV Maze shows three ways, and only the third is a genuine
loss:

* **carried** — a `catalog.show` row still stands under the preserved TV Maze id,
  so `/shows/:id` resolves and every `app` row pointing at it survives NEU-1046.
* **dropped, with a title twin** — the copy is gone but a `tmdb_id`-bearing row
  carries the same folded title. The show is in the catalog under a different id.
* **dropped, without a title twin** — nothing shares the title. This is breadth
  TMDB does not appear to hold, and `absent_pct` is its share of the bucket.

**Read the two twin counts as a bracket, never one of them as the answer.** The
title test is biased both ways and neither cancels the other. Exact folded
equality cannot see a grain mismatch — "Cunk on Earth" against TMDB's "Cunk on…"
reads as absent — which over-reports loss. Title equality with nothing else
agreeing is meanwhile far too generous: the prune measured **6,464** title twins
against production and only **3,337** that also agreed on first-air year, so
roughly half a title-only count is probably not the same show. So each bucket
carries `dropped_with_title_twin` (the optimistic bound on what survived) and
`dropped_with_title_and_year_twin` (the pessimistic one, using enrichment's own
±1-year tolerance). `absent_pct` is derived from the generous one; the harsh
reading is `(dropped - dropped_with_title_and_year_twin) / tvmaze_shows`.

A bucket over 500 shows that is more than 50% absent is flagged `advisory` and
warned about on stderr, on **both** axes — `advisory_languages` and
`advisory_eras`. Advisory means advisory: it does not fail the run.

**The artifact is the regression check.** The JSON is deterministic (buckets
ordered by name, keys sorted), so two runs of an unchanged database are
byte-identical and `git diff` over a saved copy is what shows a bucket getting
*worse*. That needs a copy to diff against, so **commit the first production
run's artifact to `docs/migration/neu-1048-coverage-baseline.json` in the same PR
that records the run** — the same treatment and the same reason as
`reconciliation-baseline.json`, which is committed precisely so the comparison
outlives the container that produced it. A bucket that is thin today is the accepted cost; a bucket that is
thinner than last run is the regression this exists to catch.

One limit, inherited from `show_prune`: the twin test is exact folded-title
equality, so it cannot see a grain mismatch — "Cunk on Earth" against TMDB's
"Cunk on…" counts as absent. That biases the measurement toward over-reporting
loss, which is the right direction for a safety check.

### Running it

```bash
task gate:coverage > /tmp/gate.json      # go/no-go on stderr, the artifact on stdout

# production — the run that actually matters
ssh tom@ssh.neuroticsasquat.ch \
  'docker exec <tvbf-backend-container> python -m tvbf.jobs.coverage_gate' \
  > /tmp/gate.json; echo "verdict exit: $?"
```

Run it **before NEU-1046 and before NEU-1051**. The denominator is `tvmaze.show`
— the 88,971 rows the migration started from — which is only readable while that
schema still stands.
