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
| Human queue (NEU-1044) | `task queue:confirm` / `queue:reject` | after enrichment, **before ingest** | ⚠️ partial — 4 guesses confirmed 2026-08-10 and **re-stamped `'human'` 2026-08-12** when NEU-1048's gate caught that the verdict had never reached the database; 2 user-touched rows still unresolved, and the window has closed (see NEU-1066) |
| Episode-grain mapping (NEU-1045) | `task map:episodes` | after enrichment, **before ingest** | ❌ **never run — window closed.** The ingest started 2026-08-10, this merged 2026-08-11. Running it now maps nothing: 1,909,367 rows collide and 760,254 have no TMDB counterpart. Needs a re-point pass instead. |
| Full catalog ingest (NEU-1034) | `task ingest:catalog` | after copy + enrichment | ✅ 2026-08-10 → 2026-08-11 — 228,723 shows |
| Season-grain dedupe (NEU-1119) | `task dedupe:seasons` | after ingest; re-run after any delta | ✅ 2026-08-11 — 122,350 deleted, 2,125,419 episodes re-pointed |
| Show-grain prune (NEU-1066) | `task prune:shows` | after ingest | ✅ 2026-08-11 — 26,141 shows deleted over 262 batches, taking 47,443 seasons and 840,169 episodes. `catalog.show` 255,010 → 228,869; 2 unmatched rows kept. Needed the `ix_show_*_episode_to_air_id` indexes first (see below) |
| User-touched remediation (NEU-1066) | `neu-1066-user-touched-remediation.sql` | **after NEU-1046**, then re-run the prune | ⬜ unblocked 2026-08-12 — NEU-1046 deployed and the FK now points at `catalog.show` |
| Pre-cutover go/no-go (NEU-1048) | `task gate:coverage` | **before NEU-1046**, while `tvmaze` still stands | ✅ 2026-08-12 — **GO** on the second run. The first returned no-go on the four `title_year` guesses NEU-1044 confirmed by hand on 2026-08-10 and never re-stamped; `queue:confirm` re-stamped all four `'human'`, and the re-run passed every criterion. Artifact committed as `neu-1048-coverage-baseline.json` |
| `app` FK repoint (NEU-1046) | migration `b6d24f0ac715`, applied on deploy | after the go/no-go, before NEU-1126 | ✅ 2026-08-12 — all five constraints on `catalog`, `ON DELETE` unchanged, verified by `pg_get_constraintdef` |
| Episode-grain re-point (NEU-1126) | `task repoint:episodes` | **after NEU-1046**, before NEU-1047 | ✅ 2026-08-12 — 1,908,378 copied episodes deleted over 382 batches in ~7 min; re-pointed 8,387 watches, 77 ratings, 364 activity events; 0 blocked by collision. 189 user-touched episodes with no TMDB counterpart kept, as designed |
| Post-repoint acceptance test (NEU-1125) | `task reconcile:verify -- --spine catalog` | **after NEU-1046 + NEU-1047**, gates NEU-1050 and NEU-1051 | ✅ 2026-08-13 — **GO**. Exit 1 with eight discrepancies, every one of them a *gain* traceable to one user's app use after the baseline was taken (11 watches, 2 ratings, 13 events); no `LOST` line, and the null-show bucket empty on both sides. Close-of-window state committed as `neu-1125-post-repoint-snapshot.json` |
| Credits backfill (NEU-1127) | `task backfill:credits` | after the ingest; **before NEU-1051** | ✅ 2026-08-13 — run in production, confirmed by the operator. This row is the record NEU-1127 existed to create; the counts were not captured at the time, so `task backfill:credits:report` against prod is what would restate them |
| On-the-day reconciliation (NEU-1051) | `reconcile verify` in the prod container | **before** the drop | ✅ 2026-08-14 — **GO**. Exit 1 with 8 discrepancies, every one a *gain* from one user's app use after the baseline (Ted Lasso +1 watch +1 rating, Lucky +1 watch +1 rating, Arrested Development +9 watches, +13 activity events total); no `LOST` line |
| Pre-drop dump (NEU-1051) | `scripts/dump_tvmaze.sh` | **before** the drop | ✅ 2026-08-14 — 261,153,828 bytes, test-restored on prod and reconciled table-for-table across all 18 tables (3,533,911 episodes, 2,681,043 guest-cast, 485,271 people, 89,082 shows). Artifact `tvmaze-20260814T003446Z.dump` + `.counts.txt` |
| `tvmaze` drop (NEU-1051) | migration `a7e3c8d15f42`, applied on deploy | **after the credits backfill and the dump**; last of the migration | ⬜ merging the PR *is* the run — Coolify applies migrations on deploy, and the migration refuses without `TVBF_TVMAZE_DUMP_VERIFIED=yes` |
| Orphan-row retirement (NEU-1146) | `task retire:orphans` | **after** the drop; re-run after any later ingest or delta | ✅ **2026-08-14 — criterion 7 met, catalog is TMDB-sourced throughout.** 0 `tmdb_id IS NULL` rows at all three grains, verified by query. Reconciliation matched the prediction **exactly** on all six metrics; 30 discrepancies, every one enumerated in advance, no unlisted `LOST` line. Needed `neu-1146-import-ne-remediation.sql` between two runs. Earlier note: report run 2026-08-14 (artifact `neu-1146-pre-run-report.json`, and it caught the tier-2 special-offset bug fixed in #255); **the pass itself has not run.** Ends the locally-authored residue at all three grains (782,161 episodes, 18,341 seasons, 2 shows as of 2026-08-14) and is the **first pass that deliberately deletes user rows** — ~95 watches, per ADR-0012. Run `task retire:orphans:report` first and commit its loss list; `reconcile:verify` will not come back clean afterwards, by design |


## Dropping `tvmaze` (NEU-1051)

**Merging the PR is the drop.** Migration `a7e3c8d15f42` moves `ingest_run` into
`catalog` and then runs `DROP SCHEMA tvmaze CASCADE`, and Coolify applies
migrations on deploy — so there is no separate "run the pass" step to forget,
and equally no chance to take the dump afterwards. It goes 3.5M episodes,
484k people and 2.67M guest-cast rows, and no upstream can put the TV Maze
originals back.

### The migration refuses to run without the dump

`upgrade()` raises `DumpNotVerified` when `tvmaze.show` holds real rows and
`TVBF_TVMAZE_DUMP_VERIFIED` is not set to `yes`. That guard exists because a
runbook sentence is not a control: every other one-shot pass here refuses
structurally (`show_prune`'s `IngestNotRun`, then `episode_repoint`'s), and this
is the only one where merging *is* the destructive act. It stands down below
1,000 rows, so a fresh `db:init && migrate`, CI and the test suite never see it.

### Before merging

```bash
./scripts/dump_tvmaze.sh                    # dump, test-restore, reconcile — all on prod
```

Everything runs on the prod host and the dump travels once, at the end. The
script fails rather than reporting success unless the restored database matches
the source **table-for-table on exact `count(*)`**, `show`, `episode`,
`show_cast` and `episode_guest_cast` all come back non-empty, and the fetched
file matches prod's byte count exactly.

**A `--schema=tvmaze` dump is not restorable on its own**, which the first real
run of this script is how we found out. It carries the schema and nothing else,
so four of its indexes reference `public` objects that do not travel with it —
`pg_trgm` for `gin_trgm_ops`, and this repo's own `immutable_unaccent` wrapper
(`sql_fold.py`, migration `c2e451aa1ec6`). Any restore, including a real
recovery, needs these first:

```sql
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS unaccent;
CREATE OR REPLACE FUNCTION public.immutable_unaccent(text) RETURNS text
  LANGUAGE sql IMMUTABLE STRICT AS $$ SELECT public.unaccent($1) $$;
```

The script runs them and then restores with `--exit-on-error`, because without it
`pg_restore` reports "errors ignored on restore: 4" and exits having built no
indexes — a dump that passes for good while being incomplete.

Then, in order:

1. Copy `tvmaze-<stamp>.dump` and `.counts.txt` **off the VM**. A dump sitting on
   the machine whose loss it insures against is not a backup.
2. Re-run the reconciliation against the live database **on the day**. Note the
   direction: `verify --baseline -` reads the *baseline* from stdin and diffs it
   against whatever database **that process** connects to — so `verify` has to run
   **inside the prod container**, with the committed baseline piped in. Piping a
   prod `capture` into a local `verify` looks equivalent and silently checks your
   laptop's database instead.
   ```bash
   cat docs/migration/reconciliation-baseline.json \
     | ssh $PROD_SSH 'docker exec -i <tvbf-backend-container> \
         python -m tvbf.jobs.reconcile verify --baseline - --spine catalog'
   ```
   `--spine catalog` explicitly, because until this PR merges the prod image still
   defaults to `tvmaze`. Exit 1 with **gains only** is the pass — users keep using
   the app between the baseline and the drop. A `LOST` line is a stop.
3. Set `TVBF_TVMAZE_DUMP_VERIFIED=yes` in Coolify.
4. Merge. The deploy applies the migration and the schema goes.

The remaining preconditions are matters of judgement rather than commands, and
were satisfied as follows: the go/no-go passed 2026-08-12
(`neu-1048-coverage-baseline.json`), `app.watch_archive` holds 9,359 verified
rows, and the credits backfill ran 2026-08-13.

**Take `TVBF_TVMAZE_DUMP_VERIFIED` back out of Coolify once the deploy has
landed.** It has done its job, and a variable left set is one that cannot refuse
anything next time.

### What the drop is safe against

Nothing in `app` has referenced `tvmaze` since NEU-1046 repointed all five
foreign keys onto `catalog`, so the `CASCADE` has no inbound constraint to
follow. `app.watch_archive` has no foreign keys at all and describes every watch
in human terms, so it survives intact and stays the recovery path of last resort
— it is also the reason this ticket could be deferred safely for as long as it
was.

### Reverting

`downgrade()` puts `ingest_run` back in a recreated `tvmaze` schema and **does
not restore any data** — no downgrade could. A real revert is:

```bash
ssh $PROD_SSH 'docker exec -i <pg> psql -U root -d tvbf -c "DROP SCHEMA IF EXISTS tvmaze CASCADE"'
ssh $PROD_SSH 'docker exec -i <pg> pg_restore --no-owner --no-acl -U root -d tvbf' < tvmaze-YYYYMMDDTHHMMSSZ.dump
```

then `alembic downgrade -1` to move `ingest_run` back. Note the order: the
restore brings a `tvmaze.ingest_run` of its own, so the downgrade's
`ALTER TABLE catalog.ingest_run SET SCHEMA tvmaze` would collide with it —
drop the restored copy first, since the `catalog` one is the live table and
holds every run since the move.

### What went with the schema

Four passes read `tvmaze` directly and could not outlive it, so NEU-1051 deleted
them along with their tests: the catalog copy (`task copy:catalog`, NEU-1042),
the show-grain prune (`task prune:shows`, NEU-1066) whose guard was
`EXISTS (SELECT 1 FROM tvmaze.show ...)`, and the pre-cutover coverage gate
(`task gate:coverage`, NEU-1048) whose entire denominator was `tvmaze.show`.
Their production runs are recorded above and that record is now the only account
of them. `MIN_INGESTED_SHOWS` and `IngestNotRun` moved from `show_prune` into
`tmdb/episode_repoint.py`, the one pass left that asks the question.

`season_dedupe`, `episode_map`, `episode_repoint`, `enrichment` and `human_queue`
survive — they read `catalog` only. `season_dedupe` in particular should still be
re-run after any later ingest or delta. What each of them lost is the revert
path that ran through `task copy:catalog`; past this ticket, the dump above is
the only way back.

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

That post-cutover run is NEU-1125, and it has already happened — *Post-repoint
acceptance test (NEU-1125)* below records its verdict, and settles which of the
two committed artifacts a given gate compares against.

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

## Retiring the TV Maze orphan rows (NEU-1146)

The pass that ends the migration. Everything before it moved user history onto
TMDB-sourced rows where the *exact* key paired; this one handles everything that
key could not reach, and then deletes what has no counterpart at all — **user
rows included**. It is the only pass here that destroys watch records on purpose,
and [ADR-0012](../adr/0012-the-catalog-is-sole-sourced-from-tmdb.md) is the
decision that permits it.

**Read the report first. It is not optional, and it is the artifact of record.**

```bash
# local
task retire:orphans:report

# production — writes nothing, needs no TMDB credential, safe to run any time
ssh tom@ssh.neuroticsasquat.ch \
  'docker exec -i <tvbf-backend-container> python -m tvbf.jobs.orphan_retire report' \
  > docs/migration/neu-1146-pre-run-report.json
```

**The `task` targets run `docker compose exec` against the *local* container and
never touch production** — that is true of every target in this repo, and it is
worth saying once here because this is the pass where running the wrong one
matters least (it refuses locally, the dev database being far below the ingest
floor) and reading the wrong output matters most. Every figure below came from
the `ssh` form.

Coolify names the container with a random suffix that **changes on every
deploy**, so resolve it rather than hardcoding it:

```bash
ssh tom@ssh.neuroticsasquat.ch "docker ps --format '{{.Names}}' | grep '^tvbf-backend'"
```

Check the image tag is the commit you expect while you are there — the tag *is*
the SHA, which is the only proof the deploy carrying your fix has actually
landed:

```bash
ssh tom@ssh.neuroticsasquat.ch "docker ps --format '{{.Image}}\t{{.Status}}' | grep tvbf-backend"
```

Three things in it decide whether to proceed:

1. **`losses`** — every user row that would be deleted rather than moved, per
   user, per show, with episode title and air date, split by `disposition`.
   `deduplication` means the user already holds a row on the surviving twin, so
   one viewing keeps one row and **nothing is lost**; `genuine_loss` means TMDB
   does not model that content as an episode of that series. Folding the two
   together over-reports the loss by the count of two-parter halves — the
   2026-08-14 run deleted 112 watch rows and 1 rating, of which 95 were real
   losses and 18 were de-duplications. Read `basis` alongside it (below): it says
   which verdicts the pass proved and which it inferred. Diff the list against
   the spec's §6 and **commit the copy the run printed**, not the spec's; the
   figures move as deltas land.
2. **`links`** — every show link tier 2 would make, worst first. There were 166
   against production, which is more than anyone will review by hand and does not
   need to be: for an orphan carrying no user rows, tier 2 and tier 3 end in the
   same place (the row is deleted either way), so **only rows with
   `user_touched > 0` need scrutiny**. There was exactly one — Will & Grace
   `549 → 1064267`, offset 8.
3. **`rejections`** — why rows reached tier 3, split by reason rather than folded
   into one bucket: ambiguous on the orphan side, ambiguous on the ingested side,
   blank title after folding, no counterpart in the show.

Then capture a fresh reconciliation baseline, run a smoke pass, and run it:

```bash
# production, and there is no useful local equivalent for any of this
BE=$(ssh tom@ssh.neuroticsasquat.ch "docker ps --format '{{.Names}}' | grep '^tvbf-backend'")

# the baseline, immediately before — read-only
ssh tom@ssh.neuroticsasquat.ch "docker exec -i $BE python -m tvbf.jobs.reconcile capture" \
  > docs/migration/neu-1146-pre-run-baseline.json

# smoke run, then the pass
ssh tom@ssh.neuroticsasquat.ch "docker exec -i $BE python -m tvbf.jobs.orphan_retire retire --limit 100"
ssh tom@ssh.neuroticsasquat.ch "docker exec -i $BE python -m tvbf.jobs.orphan_retire retire"

# the verdict, baseline back in on stdin
ssh tom@ssh.neuroticsasquat.ch \
  "docker exec -i $BE python -m tvbf.jobs.reconcile verify --baseline -" \
  < docs/migration/neu-1146-pre-run-baseline.json
```

Note `--limit 100` takes no `--` here: that separator is go-task's way of
forwarding `{{.CLI_ARGS}}` and means nothing to the module.

`--limit` rounds **up to a show boundary** — a show is one transaction and one
link resolution — so a limit of 100 landing on Saturday Night Live retires all 89
of its orphans. It also skips the season and show phases entirely, because a
season is only deletable once its episodes are gone and a partial pass has not
established that.

Five things to know before running it in production:

1. **`reconcile:verify` will not come back clean, and that is the design.** Three
   expected discrepancy classes, every one of which must be confirmed
   line-by-line against the report: **losses** (the `genuine_loss` rows),
   **de-duplications** (the `deduplication` rows), and **gains** — this is the one
   pass that *creates* `app` rows, adding a show to a user's My Shows when their
   history moves to a series they did not track, plus whatever ordinary app use
   has added since the baseline. **A `LOST` line that is not on the report's loss
   list is a stop.**
2. **It is not reversible.** The pre-drop `tvmaze` dump is the only source for the
   deleted catalog rows and it cannot restore `app` rows at all. What can is
   `app.watch_archive` — a human-readable snapshot of every watch and rating, no
   foreign keys, unaffected by the schema drop. Recovery is by hand, from there.
3. **Criterion 7 is a query, and the frontend half waits on it.** After the pass,
   `catalog.episode`, `catalog.season` and `catalog.show` must hold **zero** rows
   with `tmdb_id IS NULL`; the run says so itself and logs a warning naming what
   survived if not. The TVmaze CC BY-SA credit only comes out of
   `tvbf-frontend`'s footer once this holds in production.
4. **Re-run it after any later ingest or delta.** There is no watermark — a row
   leaves the work list by being re-pointed or deleted — and a delta can add an
   ingested episode that gives a fresh orphan a twin. Tier 0 found 880 such rows
   on 2026-08-14 that had accrued since NEU-1126 ran two days earlier, which is
   the same property `season_dedupe` carries and the reason tier 0 exists here at
   all rather than being assumed spent.
5. **A show it could not delete is reported, not worked around.**
   `import_ne.show_resolution` references `catalog.show` with **NO ACTION**, and
   those staging rows are an import audit trail rather than ours to rewrite — so
   a referenced orphan show is skipped and named in `shows_kept_referenced`. The
   same applies to a show still holding catalog rows, which would otherwise
   cascade over ingested episodes.

**The pre-run report, run against production 2026-08-14** and committed as
`neu-1146-pre-run-report.json`. These are the figures to diff the pass against —
not the spec's, which came from a hand-written SQL probe that approximated the
matcher rather than being it:

| Tier | Rule | Orphans | User-touched |
| -- | -- | --: | --: |
| 0 | Exact `(season, number)`, 1:1 | 880 | 0 |
| 1 | Same show, folded title unique both sides | 116,911 | 64 |
| 2 | Cross-show, `(season − offset, episode_number)` | 3,565 | 17 |
| 2b | Title fallback inside a linked pair | 1,511 | 0 |
| 3 | **Delete** | 659,294 | 108 |

At row grain: **79 watch rows move; 112 watch rows and 1 rating are deleted** —
**95 a genuine loss and 18 a de-duplication** a surviving twin already records —
and **1 `user_show_watch` row is created**.

Each loss row carries a `basis`, and it is the field to triage on, because the
two verdicts are not equally strong:

| `basis` | Rows | Meaning |
| -- | --: | -- |
| `no_counterpart` | 94 | nothing to compare against — every one a synthetic special |
| `position_only` | 16 | rests on position **and** air date; 15 de-duplications, 1 loss |
| `matched_twin` | 3 | the pass itself paired the rows — proven redundant |

**`position_only` is where a wrong "nothing was lost here" would hide**, so check
those first. All 15 that read as de-duplications are visibly two-parters by title
(`Part 2`, `(2)`, `(3)` — Friends, Lost, Parks, Brooklyn Nine-Nine), which is
independent corroboration rather than the same inference restated.

The one `position_only` **loss** is the row that made this field exist. The
first report run called The Hook Up Plan `s2e7 "Plan Confiné.e.s"` a
de-duplication: TMDB's season 2 ends at episode 6, all of it aired 2019-10-11,
and the orphan is a lockdown special from 2020-08-26 whose positional neighbour
is an unrelated finale the user watched independently. A merged two-parter airs
in one slot, so the absorbing row carries the orphan's air date; requiring that
separates a real merge from an orphan sitting past the end of a season. **The
date is consulted there and nowhere else** — it is noise for matching (§2.4) and
signal for this one question — and the asymmetry runs the safe way, reporting a
loss that may not be one rather than the reverse.

A blanket date test would have been wrong, which is what `matched_twin` records.
Shrinking's `s3e12` carries a one-day skew (NEU-1145's Apple TV+ issue) but the
matcher *paired* it with `s3e11` on identical titles, so its redundancy is proven
and no date test applies. 166 links proposed, 4 dropped for
carrying more than one candidate, and exactly **one carries user data**: Will &
Grace `549 → 1064267`, offset 8, 17 user-touched. That is the only link needing
review, per the reasoning above.

All three links the acceptance criteria name resolve automatically, each by a
different route — which is why all three routes exist:

| Link | Basis | Offset |
| -- | -- | -- |
| Will & Grace `549 → 1064267` | title + airdate evidence (48 pairs) | 8 |
| Cunk on Earth `63900 → 1067768` | aggregate episode-title evidence (5 titles) | none — tier 2b |
| Discretion `87519 → 1202502` | sole same-folded-name sibling | none — no episodes |

**The first run of this report found a bug, and it is the one worth knowing
about.** It rescued 16 of Will & Grace's 17 user-touched rows — the outcome the
sixth acceptance criterion names as a failure. 47 of the 48 evidence pairs gave
offset 8 and one gave 11: orphan `s11e-1`, a synthetic special, against TMDB's
`s0e1`. NEU-1042 numbered TV Maze specials negative *within their original
season* while TMDB parks them in season 0, so a special's season relationship is
precisely the one that does not follow the series' — and a single such pair was
enough to make a unanimous offset read as inconsistent, skip tier 2, and drop the
show to a title fallback that cannot pair `"Eleven Years Later"` with
`"11 Years Later"`. Specials are now excluded from the offset derivation; the
consistency rule is unchanged and still takes no vote. If a future change makes
tier 2 quietly stop placing a show, this is the shape of the bug to look for.

### The expected reconciliation, enumerated before the run

Baseline captured from production 2026-08-14, immediately before the pass, and
committed as `neu-1146-pre-run-baseline.json`. §7 requires the discrepancies to
be enumerated **in advance** rather than rationalised afterwards, so here they
are:

| Metric | Baseline | Delta | Expected after |
| -- | --: | --: | --: |
| `episode_watches` | 8,584 | −112 | 8,472 |
| `episode_ratings` | 80 | −1 | 79 |
| `activity_events` | 819 | −7 | 812 |
| `tracked_shows` | 621 | **+1** | 622 |
| `show_ratings` | 97 | 0 | 97 |
| `users` | 5 | 0 | 5 |

**Two things this table shows that the report does not, and both would otherwise
read as unexplained losses.**

**The report's loss list does not cover `activity_events`.** It enumerates watch
and rating rows, because those are what a user would describe as their history —
but the harness counts events as a sixth metric, so the pass drops seven of them
and criterion 4's "every `LOST` line must appear on the report's loss list"
cannot be checked against that metric. Nine events ride on orphan episodes; seven
sit on rows the loss list already names (Sex and the City ×2, The Bear ×3, The
Hook Up Plan, Unbreakable Kimmy Schmidt) and are deleted with them. The other two
**move**, and are worth knowing about because their absence from the loss list
looks like an omission and is not: Mr. Robot's `s2e-1 "Mr.Robot_dec0d3d.doc"` and
The Expanse's `s1e-1 "The Expanse Expanded"` are synthetic specials whose titles
match a TMDB season-0 special exactly, so tier 1 pairs them and their history
survives.

**A cross-show move appears as a loss *and* a gain.** The harness keys on
`(user, show)`, so the 17 Will & Grace watches moving from `549` to `1064267`
produce a `LOST` line on the original and a `GAINED` line on the revival. Nothing
is lost — it is the same 17 rows under the show TMDB models them as belonging to
— but a reader scanning for `LOST` will find it, and it is the single largest one
in the diff. Check that the two sides balance before reading it as anything else.

### What the run actually did — 2026-08-14

Two runs, with a hand-applied remediation between them.

**First run.** Cleared both lower grains completely — 0 orphan episodes, 0 orphan
seasons — and stopped at the show grain, exiting 1:

    kept 2 orphan show(s) still referenced by import_ne staging rows or still
    holding catalog rows: 63900, 87519
    2 orphan row(s) remain — 0 episode(s), 0 season(s), 2 show(s)

Both were empty husks by then: 0 episodes, 0 seasons, **0 user rows of any kind**
— the show-grain links had already moved every `user_show_watch` onto its
counterpart. The sole blocker was one `import_ne.show_resolution` row apiece, and
`show_resolution_show_id_fkey` is the **only NO ACTION foreign key into
`catalog.show`**; the other 23 all CASCADE. That exit code is the design working:
the pass will not rewrite another subsystem's records, so it reported and
refused rather than cascading over them.

**The remediation.** `neu-1146-import-ne-remediation.sql` re-points those two
staging rows onto the linked shows, guarded by preconditions that refuse unless
the database is in exactly the state described. Applied 2026-08-14.

**Second run.** Deleted the two shows and exited 0 — *"no orphan rows remain at
any grain — the catalog is TMDB-sourced throughout"*. Confirmed independently
rather than taken on the pass's word:

```sql
SELECT 'episode', count(*) FROM catalog.episode WHERE tmdb_id IS NULL
UNION ALL SELECT 'season', count(*) FROM catalog.season WHERE tmdb_id IS NULL
UNION ALL SELECT 'show',   count(*) FROM catalog.show   WHERE tmdb_id IS NULL;
-- 0 | 0 | 0
```

### The reconciliation matched the prediction exactly

30 discrepancies, and **every one was enumerated before the run**:

| Metric | Net | Predicted |
| -- | --: | --: |
| `episode_watches` | −112 | −112 |
| `episode_ratings` | −1 | −1 |
| `activity_events` | −7 | −7 |
| `tracked_shows` | +1 | +1 |
| `show_ratings` | 0 | 0 |

**No unlisted `LOST` line** — criterion 4 holds. Every show's loss matches the
committed pre-run list row for row: Saturday Night Live 60, Parks 15, Friends 12,
Lost 8, Brooklyn Nine-Nine 4, and each single-row tail down to And Just Like
That. The three shapes flagged in advance all behaved as described: Will & Grace
showed `LOST 17` on `549` against `GAINED 17` on `1064267` (the move, balanced);
the seven activity events were exactly the seven predicted; and `tracked_shows`
netted +1 from two moves plus the one row §4.3 creates.

### One thing to not misattribute later

**323 episodes across 15 shows carry a null `season_id`, and this pass created 2
of them.** The rest predate it. The pass counts what it does here directly —
`episodes_left_without_season`, taken after the re-point and before the delete —
and reported 2, which is an orphan season having no ingested counterpart of that
number for its episodes to inherit.

None of the 323 carries a user watch or rating, and 229 of them still have a
season row for their number, so the read paths are unaffected either way:
`catalog/seasons.py` builds the season list from `catalog.season`, and an episode
keeps its `show_id` and `season_number` regardless. The remaining 94 are a
pre-existing data-quality question worth its own ticket, not evidence about this
one.

## Credits backfill (NEU-1127)

The third instance of *merged is not run*, and the reason the row above exists.

The full catalog ingest finished on 2026-08-11 and the two credit writers merged
later the same morning — NEU-1039's `_write_credits` at 04:54 UTC, NEU-1040's
`_write_episode_credits` at 05:24. `aggregate_credits` had been in
`DEFAULT_APPEND` since 2026-08-09 and the season blocks have always carried
`guest_stars` / `crew`, so every payload the ingest fetched *contained* the
credits. There was nothing to write them to.

Measured against production 2026-08-12:

| `catalog` table | rows |
| -- | -- |
| `show_cast` | 0 |
| `show_crew` | 0 |
| `episode_guest_cast` | 0 |
| `episode_crew` | 0 |
| `person` | 0 |
| `show` where `tmdb_synced_at IS NOT NULL` | 228,841 |

Since NEU-1047 repointed the read paths, `GET /shows/{id}/cast`, `/crew`,
`/episodes/{id}/guest-cast` and `/episodes/{id}/crew` return `[]` for every show
and `/people/{id}/credits` returns four empty arrays for every person. The read
path is correct — a show ingested after the writers merged serves its credits
fine. There is simply nothing to read.

### Why re-running the ingest is not the fix

Its work list is `tmdb_synced_at IS NULL` and all 228,841 rows are stamped, so
the window has shut exactly as it had for NEU-1045. Clearing the stamp and
re-running the full pass was rejected: same 8.7 hours, but it rewrites 228k shows
and 6.5M episodes to recover four tables that ride the same request, and every
one of those writes is a chance to disturb a spine users are now reading from.

The daily delta *does* write credits — `mirror_series` is shared — but it visits
only shows TMDB reports as changed, so it will never work through the backlog.

### The watermark is a column

`catalog.show.credits_synced_at`, added by migration `c9a3f1e60b47`. The
alternative work list — *the show carries no `show_cast` row* — needs no
migration and is self-clearing the way `enrichment.py`'s is, but it cannot
represent the outcome the acceptance criteria call normal: a show TMDB has no
credits for looks identical to one nobody has fetched, so every credit-less
series is re-fetched on every run and the pass never converges.

`mark_series_synced` stamps both columns, so a show the delta has already covered
never enters the backlog. `mark_credits_synced` stamps only the one, because the
backfill writes no spine and is in no position to claim the ingest's watermark.

### Running it

```bash
# The artifact, before and after. Writes nothing, needs no TMDB credential.
ssh "$PROD_SSH" 'docker exec -i <tvbf-backend> python -m tvbf.jobs.credits_backfill report' \
  > /tmp/credits-before.json

# A hundred shows first — ~15 seconds, and it proves the credential and the
# writers against production data before 8.7 hours are committed to.
ssh "$PROD_SSH" 'docker exec -i <tvbf-backend> python -m tvbf.jobs.credits_backfill backfill --limit 100'

# The pass. Resumable per show, so kill it freely and start it again.
ssh "$PROD_SSH" 'docker exec -i <tvbf-backend> python -m tvbf.jobs.credits_backfill backfill'
```

Exit 0 means the pass completed; 1 means it aborted or raised. A show with no
credits upstream is stamped like any other and counted apart — it is not a
failure, and it is what stops the next run fetching it again. Shows that failed
for any other reason are left unstamped, so re-running picks exactly those up.

The report's `user_touched_without_credits` is the spot-check list the
acceptance criteria ask for, worst first by tracker count. It should be empty
afterwards but for shows TMDB genuinely has no cast for.

### What it must not do, and how that is enforced

The pass writes the four credit tables and their three lookups (`person`,
`character`, `crew_role`) and nothing else. That guarantee lives in
`upsert.write_series_credits` rather than in the job: the caller never assembles
it out of private writers, so there is one place to read to know it holds. The
episode surrogate ids the episode-grain writer needs come from a query rather
than from an `upsert_episodes` the pass deliberately does not perform — which
also means an episode TMDB has and the mirror does not is skipped, since
inserting it would make this a spine pass by the back door.
`test_writes_nothing_outside_the_credit_tables` proves it the way the ticket
asks: spine row counts and the show's own columns, either side of a run.

A per-show failure rolls back before it is counted, so a stamped show always
carries a complete credit set and a re-run never has to wonder whether it does.

## Show-grain prune (NEU-1066)

> **Retired by NEU-1051.** The pass below no longer exists — it read `tvmaze`
> directly and was deleted with that schema. This section stays as the record
> of what it did, which is now the only account of it.

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

> **Retired by NEU-1051.** The pass below no longer exists — it read `tvmaze`
> directly and was deleted with that schema. This section stays as the record
> of what it did, which is now the only account of it.

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
*worse*. That needs a copy to diff against, which is
`neu-1048-coverage-baseline.json` beside this file — the same treatment
`reconciliation-baseline.json` gets and for the same reason: the comparison has
to outlive the container that produced it. **Overwrite it from any later
production run** and let `git diff` be the review.

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

### The first production run (2026-08-12)

**NO-GO, then GO.** The first run failed one criterion, and on exactly the case
the criterion was widened to catch: four shows carrying `match_method =
'title_year'` — *Dr. Brain*, *Monsters: The Lyle and Erik Menendez Story*, *You
Are What You Eat: A Twin Experiment* and *The Traitors Ireland Uncloaked* (12
episode watches). All four were confirmed correct **by hand on 2026-08-10** and
the verdict never reached the database, which NEU-1044's own description
predicted in as many words: *"the rows still read `match_method = 'title_year'`,
indistinguishable from an unreviewed guess."* Four `queue:confirm` calls
re-stamped them `'human'` — the operation `human_queue` describes as the point
rather than a no-op — and the re-run passed every criterion.

Worth keeping, because it is the argument for the criterion being the queue's and
not a narrower one: an unresolved verdict that lives only in a ticket comment is
invisible to everything except a gate that asks the database.

| | |
| -- | -- |
| TV Maze shows | 89,039 |
| carried | 62,884 (62,882 matched) |
| dropped | 26,155 — 6,469 with a title twin, 3,343 of those also agreeing on year, 19,686 with no counterpart |
| advisory | **Russian**, 69.4% absent (6,379 shows, 1,480 carried) |
| eras | none flagged |

The Russian bucket is the long tail ADR-0007 flagged without measuring, now
measured. It is NEU-1066's accepted cost rather than a regression — the point of
committing the artifact is that the *next* run can tell those apart.

## Repointing `app` onto `catalog` (NEU-1046)

The cutover's load-bearing DDL, and the shortest pass in the project: five
`ALTER TABLE`s, no row rewritten. `app.user_show_watch.show_id`,
`app.user_show_rating.show_id` and `import_ne.show_resolution.show_id` move from
`tvmaze.show.id` to `catalog.show.id`; `app.user_episode_watch.episode_id` and
`app.user_episode_rating.episode_id` move from `tvmaze.episode.id` to
`catalog.episode.id`. Every `ON DELETE` behaviour is carried across verbatim.

It is **a migration, not a task** — `b6d24f0ac715`, applied by the `alembic
upgrade head` the container's `CMD` runs on start. There is nothing to trigger
and nothing to poll; deploying is running it.

### That means a failed assertion keeps the container down

The migration anti-joins each column before touching it and raises with the
count if anything does not resolve. That is deliberate — the `ALTER TABLE` would
fail anyway, but naming one row mid-window tells an operator neither the scale
nor the fix. The cost is that the failure lands on `alembic upgrade head`, and
`CMD` is `alembic upgrade head && exec uvicorn …`, so the app does not come up.

The window that could produce one is real but narrow: the TV Maze daily keeps
adding episodes until NEU-1050, and a user can watch one the copy never
mirrored. **So check immediately before deploying, not only at the go/no-go:**

```bash
# production — the same anti-join the migration runs, minus the raise
ssh "$PROD_SSH" 'docker exec -i <pg> psql -U root -d tvbf -tAc "
  SELECT '\''user_show_watch'\'', count(*) FROM app.user_show_watch w
    LEFT JOIN catalog.show s ON s.id = w.show_id WHERE s.id IS NULL
  UNION ALL SELECT '\''user_show_rating'\'', count(*) FROM app.user_show_rating r
    LEFT JOIN catalog.show s ON s.id = r.show_id WHERE s.id IS NULL
  UNION ALL SELECT '\''user_episode_watch'\'', count(*) FROM app.user_episode_watch w
    LEFT JOIN catalog.episode e ON e.id = w.episode_id WHERE e.id IS NULL
  UNION ALL SELECT '\''user_episode_rating'\'', count(*) FROM app.user_episode_rating r
    LEFT JOIN catalog.episode e ON e.id = r.episode_id WHERE e.id IS NULL
  UNION ALL SELECT '\''show_resolution'\'', count(*) FROM import_ne.show_resolution x
    LEFT JOIN catalog.show s ON s.id = x.show_id
    WHERE x.show_id IS NOT NULL AND s.id IS NULL;"'
```

All five zero → deploy. Any of them non-zero → `task copy:catalog` first; it is
idempotent, it takes 44s, and filling the missing rows is exactly what it does.
`task gate:coverage` asks the same question as its `fk_targets_resolve`
criterion, so a fresh **GO** is equally good evidence.

### Verifying afterwards

The acceptance criterion is that the definitions differ in nothing but the
schema, so read them rather than trusting the migration:

```bash
docker exec -i <pg> psql -U root -d tvbf -tAc "
  SELECT n.nspname||'.'||t.relname||' | '||c.conname||' | '||pg_get_constraintdef(c.oid)
  FROM pg_constraint c
  JOIN pg_class t ON t.oid = c.conrelid
  JOIN pg_namespace n ON n.oid = t.relnamespace
  WHERE c.contype = 'f' AND n.nspname IN ('app','import_ne')
    AND c.conname IN ('fk_usw_show','fk_user_show_rating_show','fk_uew_episode',
                      'fk_user_episode_rating_episode','show_resolution_show_id_fkey')
  ORDER BY 1;"
```

Expected, and verified against a dev database either side of an
`upgrade`/`downgrade`/`upgrade` round trip:

```
app.user_episode_rating | fk_user_episode_rating_episode | FOREIGN KEY (episode_id) REFERENCES catalog.episode(id) ON DELETE CASCADE
app.user_episode_watch  | fk_uew_episode                 | FOREIGN KEY (episode_id) REFERENCES catalog.episode(id) ON DELETE CASCADE
app.user_show_rating    | fk_user_show_rating_show       | FOREIGN KEY (show_id) REFERENCES catalog.show(id) ON DELETE CASCADE
app.user_show_watch     | fk_usw_show                    | FOREIGN KEY (show_id) REFERENCES catalog.show(id) ON DELETE CASCADE
import_ne.show_resolution | show_resolution_show_id_fkey | FOREIGN KEY (show_id) REFERENCES catalog.show(id)
```

The reconciliation re-run that proves nobody lost anything is **NEU-1125's**,
not this migration's — it is the acceptance test for the whole window rather
than for one `ALTER TABLE`, and it wants to run once the repoint and NEU-1126's
episode-grain re-point have both landed.

### Reverting

`alembic downgrade -1` puts all five back on `tvmaze`, with the same assertion
run against that spine first. It is reversible only while `tvmaze` still stands,
which it does: dropping it is NEU-1050's, deliberately separate and later, and
that gap is what leaves the old mirror as a recovery path.

### What this unblocks

`neu-1066-user-touched-remediation.sql` was blocked on the FK pointing at
`tvmaze.show`; it can run once this has. NEU-1126's episode-grain re-point and
NEU-1047's read-path switch both sit behind it too.

## Episode-grain re-point (NEU-1126)

The pass that finally puts user history on TMDB-sourced rows, and **the only one
in this project that writes to `app`.** `season_dedupe` and `show_prune` both go
out of their way not to; this one has to, because the user rows are the point.

NEU-1045 built the mapping that would have made this unnecessary and merged a day
*after* the full ingest started, so its window had already shut — the failure this
run log exists for. By 2026-08-11 all 7,137 watched-or-rated episodes pointed at
copied rows carrying TV Maze data, and running `task map:episodes` would have
mapped zero of them while spending ~62,000 TMDB requests.

### Ordering

**After NEU-1046, before NEU-1047.** The foreign keys have to reference
`catalog.episode` first: every ingested episode id is far above TV Maze's range
and absent from `tvmaze.episode`, so the update would be rejected against the old
constraint. And it has to land before the read paths move, or the duplicated grain
is visible to users on every show and season page.

### This pass makes watch history invisible until NEU-1047 ships

**Read this before running it.** The re-point moves `app.user_episode_watch`
onto *ingested* episode ids, and every read path in the app still resolves
episodes through `tvmaze` until NEU-1047 moves them to `catalog`. An ingested id
does not exist in `tvmaze.episode`, so the join finds nothing and a user's watch
history renders **empty** — on My Shows, Watch Next, every season and episode
page.

Nothing is lost: the rows are all there, `reconcile verify` passes, and the
history reappears the moment the read paths move. But between this pass and
NEU-1047 the app is visibly broken for anyone using it, which is why the project
spec scopes this to a window — *"take the app down, repoint `app.*` FKs, bring
it up"* — and why NEU-1125 says these run "inside the window, before the app is
handed back to users."

It was run against a live site on 2026-08-12 and both active users lost sight of
their history until NEU-1047 landed. The sequencing note "must land before
NEU-1047" is a constraint on *ordering*, not a statement that the gap between
them is safe to sit in.

### The production run (2026-08-12)

Ran clean, and every figure came out where the pre-flight report said it would.

| | |
| -- | -- |
| copied episodes deleted | 1,908,378 over 382 batches, ~7 minutes |
| watches / ratings / activity events re-pointed | 8,387 / 77 / 364 |
| blocked by collision | **0** |
| user-touched episodes now on ingested rows | 6,948 |
| user rows still on copied episodes | 189 — the no-counterpart residue, kept by design |
| copied episodes remaining | 782,161 = 781,266 + 889 + 6, exactly the report's three kept buckets |
| orphaned `app.activity_event` rows | **0** — the polymorphic site with no foreign key, which is the one that would have failed silently |
| total episode watches | 8,578 before and after |

`reconcile verify --spine catalog` returned **"nothing moved"** against a snapshot
captured immediately before the pass.

**That snapshot, not the committed baseline, is the instrument — and the reason is
worth recording.** `docs/migration/reconciliation-baseline.json` was captured
2026-08-11 and production had since gained 9 episode watches and 9 activity events:
a real user marked *Arrested Development* 1x16–2x2 watched at 02:01 on 2026-08-12.
Verifying against it would have failed on a gain that has nothing to do with this
pass, and the harness fails as loudly on gains as on losses by design.

**Superseded 2026-08-13 — do not re-capture the committed baseline.** This
paragraph originally concluded that NEU-1125 needed a re-captured baseline to
match against. It does not, and doing it would have destroyed the only record of
the pre-cutover state: NEU-1125 ran against the committed file, exited 1 on those
same user gains and three more, and read the verdict off the *direction* of every
line instead. See *Post-repoint acceptance test (NEU-1125)* below. What was right
here is the narrower claim: a snapshot taken immediately before a pass is the
right instrument for **that pass**, and is not the cutover gate.

A `--limit 100` smoke run preceded the full pass, per the section below: it deleted
100, moved `repointable` from 1,908,478 to exactly 1,908,378, and reconciled clean.

**Re-run the report after a large delete only once `ANALYZE` has caught up.** The
pass leaves ~1.9M dead tuples and the report's whole-table aggregates slow to
minutes until statistics refresh; `ANALYZE catalog.episode` fixes it.

### Run the report first

```bash
# local
task repoint:episodes:report

# production — writes nothing, needs no credential
ssh "$PROD_SSH" 'docker exec -i <tvbf-backend-container> \
  python -m tvbf.jobs.episode_repoint report' > /tmp/neu-1126-before.json
```

Measured against production on 2026-08-12, before the pass:

| | rows |
| -- | -- |
| `repointable` | 1,908,478 |
| `watches_to_move` | 8,387 |
| `ratings_to_move` | 77 |
| `activity_to_move` | 364 |
| `user_touched_repointable` | 6,948 |
| `user_touched_kept` | 189 |
| `kept_no_counterpart` | 781,266 |
| `kept_ambiguous_copies` | 889 |
| `kept_ambiguous_twins` | 0 |
| `kept_under_unmatched_show` | 6 |

The two user-facing counts measure different things and both are worth reading:
**6,948 is distinct episodes**, 8,828 is the rows on them (8,387 watches + 77
ratings + 364 events), because several users watch the same episode. The 364
matches the activity-event figure the ticket predicted exactly.

**The ticket predicted the ambiguity backwards, and the measurement is why that
matters.** It expected keys with more than one *ingested* twin; production has
none. What it has is 443 keys where two or more *copied* rows share a single twin
— TV Maze's own duplicate numbering arriving through NEU-1042's copy. That
direction is the dangerous one: re-pointing both copies onto one twin merges two
watch records into one, which `(user_id, episode_id)` would either reject or
silently absorb, and the reconciliation harness would read the missing row as a
loss. Both directions are refused.

### Running it

```bash
# a hundred first, then read the report again
task repoint:episodes -- --limit 100

# the full pass
task repoint:episodes

# production
ssh "$PROD_SSH" 'docker exec -i <tvbf-backend-container> \
  python -m tvbf.jobs.episode_repoint repoint'
```

Idempotent and re-runnable: a row leaves the work list by being deleted. There is
no persisted cursor, so a re-run starts from the beginning and finds only what is
genuinely still there. It **refuses to run before the full ingest** — under
150,000 rows carrying `tmdb_synced_at` it logs `IngestNotRun` and exits 1 rather
than reporting a clean grain having moved nothing.

### The deletion cost, re-checked

See *Deleting episodes needs two indexes that did not exist* above for the
original incident. NEU-1066 hit a 60-second-per-show wall deleting episodes,
because
`catalog.show.last_episode_to_air_id` and `next_episode_to_air_id` are
`ON DELETE SET NULL` foreign keys into `catalog.episode` and had no index, so
every cascaded delete seq-scanned all 255,010 shows. Migration `f85a608ef19e`
fixed it, and this pass deletes ~1.9M episodes directly — so the plan was checked
against production rather than assumed, on 2026-08-12:

* `ix_show_last_episode_to_air_id` and `ix_show_next_episode_to_air_id` are both
  present.
* The two tables that cascade from `catalog.episode` are covered:
  `uq_egc_episode_person_character` and `uq_episode_crew_episode_person_role` both
  **lead on `episode_id`**, which is what the cascade needs (a covering index that
  led on anything else would not help it).
* The cascade is **empty in practice**, which is the reassuring half and worth
  stating: NEU-1042's copy writes only `show`, `season`, `episode` and `show_aka`,
  so a copied episode carries no `episode_guest_cast` or `episode_crew` rows at
  all. Only the ingest writes those, and its rows hang off the twin that survives.
* `EXPLAIN` on the batch `DELETE` is index scans throughout — `episode_pkey` for
  the batch, `show_pkey` for the guard, `ix_activity_event_target` and
  `user_episode_watch_pkey` for the reference checks. The only sequential scan is
  over `app.user_episode_rating`, which has 78 rows and is hashed.

### What it deliberately keeps

* **The 189 user-touched episodes with no TMDB counterpart.** ADR-0008's
  locally-authored rows; deleting one destroys history nothing can restore.
* **Copied specials.** NEU-1042 numbered TV Maze's null-numbered specials
  *negative* within their season, and no ingested row carries a negative
  `episode_number` — so they find no twin and stay, without needing a special case.
* **Both sides of an ambiguous key**, in either direction.
* **A copy whose user rows could not move.** Each of the three write sites has a
  uniqueness constraint the re-point can collide with, so a user holding rows on
  *both* the copy and the twin keeps both, counted as `blocked_by_collision`.
  Production has zero of these; the constraints make the state representable,
  which is the only reason it is handled.

`still_doubled` comes back with **1,600 keys and none of them carrying user
data** — the residue this pass cannot reach, which is what the "no show carries
two rows" criterion actually scores against. A zero in `repointable` says the pass
has nothing left to do, not that the grain is clean, and the two are easy to
confuse. The list is ~1,600 JSON objects, so redirect it rather than reading it in
a terminal.

### Verifying afterwards

```bash
# `repointable` should be 0; `still_doubled` is the criterion's real scoreboard
task repoint:episodes:report

# the acceptance test: counts per (user, show) must be identical
task reconcile:verify -- --baseline - --spine catalog < docs/migration/reconciliation-baseline.json
```

Re-pointing moves a watch *within* a show, so every per-`(user, show)` count comes
out unchanged — which is what lets NEU-1125 pass on the far side of this pass, and
is asserted directly in
`tests/integration/tmdb/test_episode_repoint.py::test_reconciliation_counts_are_unchanged_by_the_re_point`.

### Reverting

`task copy:catalog` restores the deleted episode rows under their original ids but
**never touches `app`**, so the revert is a second statement per write site while
`tvmaze` stands (NEU-1051 has not run) — the same two-statement shape NEU-1119
needed. The module docstring of `tvbf/tmdb/episode_repoint.py` carries the SQL,
which re-derives the pairing in reverse and repeats the forward pass's ambiguity
and collision guards, because it needs them for the same reasons.

Step one works because `copy_to_catalog` restores **seasons before episodes**, so
the `season_id` a restored episode carries has a parent to point at even though
NEU-1119 deleted 122,350 of those season rows. That is not a claim to take on
trust: `test_the_pass_is_reversible_from_tvmaze` in
`tests/integration/tmdb/test_episode_repoint.py` runs the whole round trip.

The revert is **wider than one run of this pass** — it matches every user row on
an ingested episode with a copy beneath it, because nothing records which rows a
given batch moved. That is right for undoing the migration and wrong for undoing
a batch; `--limit` is what exists for the latter.

## Post-repoint acceptance test (NEU-1125)

The project spec's acceptance test — *"a migration that cannot produce that proof
does not ship."* The NEU-1030 harness, re-run against the **committed** baseline
with `--spine catalog`, after NEU-1046 moved the foreign keys and NEU-1047 moved
the reads. It proves nothing was lost while the window was open, and it is what
NEU-1050's code removal and NEU-1051's `DROP SCHEMA` are gated on. Nothing gets
deleted on the strength of a check that ran before the thing being verified had
happened.

```bash
ssh tom@ssh.neuroticsasquat.ch \
  'docker exec -i <tvbf-backend-container> python -m tvbf.jobs.reconcile verify --baseline - --spine catalog' \
  < docs/migration/reconciliation-baseline.json
```

### The production run (2026-08-13)

**Exit 1 — eight discrepancies, all of them gains, no loss anywhere. Verdict:
GO.** Recorded here in full, because a non-zero exit adjudicated by hand is worth
exactly as much as the argument written down beside it.

| | |
| -- | -- |
| baseline | `docs/migration/reconciliation-baseline.json`, captured 2026-08-09 and re-captured 2026-08-11 after the prune |
| spine | `catalog` |
| deployed image | `1e7e51339af5a20d8c3716d2a1628b6acc74e8e1` — the tip of `release/v0.3.0`, which is downstream of NEU-1047 rather than being it |
| run at | 2026-08-13 22:15 UTC |
| exit code | 1 |

**The ticket assumed a frozen window and there was not one.** It scopes the run
as *"inside the window, before the app is handed back to users"*; in fact the app
stayed up and writable throughout, which is why gains exist at all. Recording that
matters more than the gains do: the ticket's own instrument — *"a discrepancy in
either direction blocks the window"* — is calibrated for a database nobody is
writing to, so reading its verdict correctly here means knowing that premise did
not hold. It is also why the successor baseline below exists.

The eight lines as the harness emitted them, in its own order — ascending by
delta, then user, then show — with the one substitution this file requires:
`describe` names the user by **email**, resolved live so the artifact need not
carry it, and the record keeps the **user id** in its place for exactly the reason
the artifact does. Nothing else is edited.

```
GAINED 1 episode_ratings — c558c2bb-499f-4641-9d72-49270d7ff52a — Ted Lasso (id 44458) (baseline 1, current 2)
GAINED 1 episode_watches — c558c2bb-499f-4641-9d72-49270d7ff52a — Ted Lasso (id 44458) (baseline 35, current 36)
GAINED 1 episode_ratings — c558c2bb-499f-4641-9d72-49270d7ff52a — Lucky (id 81228) (baseline 5, current 6)
GAINED 1 episode_watches — c558c2bb-499f-4641-9d72-49270d7ff52a — Lucky (id 81228) (baseline 5, current 6)
GAINED 2 activity_events — c558c2bb-499f-4641-9d72-49270d7ff52a — Ted Lasso (id 44458) (baseline 3, current 5)
GAINED 2 activity_events — c558c2bb-499f-4641-9d72-49270d7ff52a — Lucky (id 81228) (baseline 10, current 12)
GAINED 9 activity_events — c558c2bb-499f-4641-9d72-49270d7ff52a — Arrested Development (id 321) (baseline 17, current 26)
GAINED 9 episode_watches — c558c2bb-499f-4641-9d72-49270d7ff52a — Arrested Development (id 321) (baseline 15, current 24)
```

**One user id across all eight, which the committed artifacts let anyone
re-derive**: that user's totals move 2,764 → 2,775 watches, 78 → 80 episode
ratings and 343 → 356 events between the two files, and no other user's totals
move at all. The record is checkable from the repo rather than taken on trust.

**Every line reads GAINED, and that is the proof.** `compare` reports both
directions, so a harness that found a loss would have said so; eight gains and no
`LOST` line is the no-data-lost statement the ticket asks for, stated by the
instrument rather than inferred from totals.

**The gains are one user's ordinary app use after the baseline was taken**, not
something that ran during the window. Each row carries a timestamp later than the
2026-08-11 re-capture, and each pairs with the activity event the write path
emits alongside it — nine `watched_episode` events for nine watches on 2026-08-12
02:01 UTC (a binge marked over 21 seconds), and a `rated_episode` + a
`watched_episode` for each of the two rate-then-mark pairs on 2026-08-13 01:37
and 02:20 UTC. 11 watches, 2 ratings, 13 events: the eight lines add up to
exactly those rows and to nothing else.

`tracked_shows` (621) and `show_ratings` (97) are **unchanged**, and the
`show_id: null` bucket is **empty on both sides** — no watch, rating or event
lost the episode row that resolves it to a show. That second one is the check the
LEFT joins exist for, and it is the one a totals comparison would miss.

### Adjudicating a gain, without reinventing the query

A gain is only benign if the rows behind it are newer than the baseline. The
harness counts and does not date, so that question is answered against the live
database — the query is here so the next person does not write it from scratch at
2am with a window open:

```sql
select 'episode_watch' as kind, e.show_id, count(*),
       min(w.watched_at), max(w.watched_at)
  from app.user_episode_watch w
  join catalog.episode e on e.id = w.episode_id
 where e.show_id in (<the shows the harness named>)
   and w.watched_at > timestamptz '<baseline capture>'
 group by 1, 2
union all
select 'event:' || a.verb, a.target_id, count(*), min(a.created_at), max(a.created_at)
  from app.activity_event a
 where a.created_at > timestamptz '<baseline capture>'
 group by 1, 2;
```

A gain whose rows predate the baseline is **not** benign and blocks the window:
it means something wrote history, which is a different failure from a user
watching television.

### Why the baseline is not re-captured to make this exit 0

It would be one command and it would destroy the proof. The committed baseline is
the only record of the pre-cutover state; replacing it with a post-cutover capture
turns the acceptance test into a tautology — a snapshot compared against itself
always passes, including after a loss. The harness's own docstring settles the
adjacent temptation for the same reason: no flag lets gains pass, because *"a
harness that warns is a harness that gets ignored during a migration window."*

The cost is that a live application drifts away from a fixed baseline, so every
future re-run also exits 1 and also has to be read. That is the intended trade —
the run is cheap and the reading is the point.

## `neu-1125-post-repoint-snapshot.json`

The close-of-window state, captured on `--spine catalog` immediately after the run
above:

```bash
ssh tom@ssh.neuroticsasquat.ch \
  'docker exec <tvbf-backend-container> python -m tvbf.jobs.reconcile capture --spine catalog' \
  > docs/migration/neu-1125-post-repoint-snapshot.json
```

| | baseline (`tvmaze`, 2026-08-11) | this snapshot (`catalog`, 2026-08-13) |
| -- | -- | -- |
| users | 5 | 5 |
| tracked shows | 621 | 621 |
| episode watches | 8,569 | 8,580 |
| show ratings | 97 | 97 |
| episode ratings | 78 | 80 |
| activity events | 802 | 815 |

**It does not replace `reconciliation-baseline.json` and must not be used as one**
— NEU-1125 is the comparison against the *pre-cutover* file, and that is the
comparison that means something. What this artifact is instead is **the baseline
NEU-1050 and NEU-1051 verify against**, and using it is what makes their gate
machine-checkable again:

```bash
ssh tom@ssh.neuroticsasquat.ch \
  'docker exec -i <tvbf-backend-container> python -m tvbf.jobs.reconcile verify --baseline - --spine catalog' \
  < docs/migration/neu-1125-post-repoint-snapshot.json
```

Neither of those passes should move a single count — retiring TV Maze code and
dropping a schema `app` no longer references are both meant to be invisible to
every number in here — so a loss caused by either shows up against the state the
window actually closed in rather than against a state three passes older, and the
user activity that makes the pre-cutover baseline exit 1 forever is already
folded in. Re-capture this file, deliberately and in its own commit, whenever a
later gate wants a fresher zero point; the pre-cutover baseline is the one that
must never be re-captured.

Same format and same determinism as the baseline, which is why `git diff` over it
is the regression check — and why it carries **no capture timestamp of its own**:
a clock in the payload would make two captures of an unchanged database differ.
Its date lives in the run log above and in the commit that added it. It holds
user ids rather than emails, which is why it can be committed at all.


## Airdate correction (NEU-1145)

`catalog.air_date_offset` records how many days one season's mirrored dates are
shifted from TMDB's, established nightly against the TV Maze oracle by
`task reconcile:airdates`. This section is about **proving it worked**, which is
acceptance criteria 2 and 3 and neither of which is a unit test.

### Capture the baseline BEFORE the first pass

Non-negotiable and unrecoverable afterwards. AC 3 makes two claims:

> the 198 Apple and 93 Prime shifted rows now agree, and the 104 + 35
> already-correct rows are untouched

A query run after the fact can see the first. Nothing but a baseline can see the
second, because a row that agrees today looks identical whether it always did or
whether something broke it and something else repaired it — and moving a
correct row a day the *other* way is exactly what the per-network rule §2.6
rejected would have done to 17 Prime Video rows.

`app.watch_archive` is append-only (NEU-1029), so the baseline keeps
indefinitely. It just has to be taken first.

```bash
# production, immediately before the first `airdate_reconcile` run — read-only
ssh tom@ssh.neuroticsasquat.ch \
  'docker exec -i <tvbf-backend-container> python -m tvbf.jobs.airdate_verify capture' \
  > docs/migration/neu-1145-airdate-baseline.json

# local
task verify:airdates:capture
```

Commit the baseline. It holds no email or display name — show name, season,
episode number and two dates per row — and it is byte-identical between two
captures of an unchanged database, so `git diff` over it is itself the check.

### The verdict

```bash
# production, baseline back in on stdin
ssh tom@ssh.neuroticsasquat.ch \
  'docker exec -i <tvbf-backend-container> python -m tvbf.jobs.airdate_verify verify --baseline -' \
  < docs/migration/neu-1145-airdate-baseline.json

# local
task verify:airdates:verify
```

**Exit 0 = nothing got worse. Exit 1 = something did.** This deliberately
inverts `reconcile verify` above, which fails on any difference in either
direction because during a cutover window nothing should move at all. Here
movement is the point — a row going from *a day early* to *agrees* is the ticket
working — so only a regression fails, in three shapes: a disagreement that grew,
a disagreement that changed sign without reaching zero (the over-correction), or
a row that stopped resolving to a catalog episode at all. That last one matters
because a shrinking denominator is how a regression hides inside an improving
percentage.

**Rows still reading a day early are printed, not scored.** Some cannot be
corrected and it is on purpose: a show TV Maze has never heard of, and a season
the trust rule refused — Shrinking S3 is the worked example, and it is in the
reconciliation's own refusal log with its per-episode deltas. Read the count;
do not treat it as a failure.

Resolution is by `(source_show_id, season_number, episode_number)` rather than
by the archived episode id: those ids are a pre-cutover snapshot and NEU-1126
and NEU-1146 both moved out from under them, while the show id survived because
NEU-1042 preserved TV Maze's ids as the catalog surrogates. An `unresolved`
count above a handful is worth reading rather than waving through.

### AC 2, which cannot be automated

```bash
task verify:airdates:shows                        # Silo, Lucky, Ted Lasso
task verify:airdates:shows -- --show "Severance"
```

The criterion is *"hand-verified against Apple's published schedule"* and there
is no machine-readable schedule to check against, so this asserts nothing. It
prints the three values a human compares: the date now served, the raw TMDB
value it was derived from, and the offset between them. A season with no offset
whose dates still look a day early is the interesting row — the reconciliation
log says which rule refused it.

Note that a bare name can match more than one show (two "Silo", three "Lucky"
locally), so every match is listed rather than one being picked.

