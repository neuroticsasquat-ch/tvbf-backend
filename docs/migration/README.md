# Migration artifacts

Fixed home for the TMDB migration's reconciliation baseline (NEU-1030) and the
one-off repair scripts the migration's production runs turn out to need. The
location is pinned here rather than improvised at cutover, because milestone 5's
go/no-go re-runs the same harness against the same file — and because a
procedure that only exists in somebody's terminal history is a procedure that
will not survive to the day it is needed.

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

Two rows are genuinely unmapped and need a decision either way: *Cunk on Earth*
(several *Cunk* entries, so the rule refused to guess) and *Discretion* (no
premiere date, excluded from tier 3 by design). Neither carries watch history.
