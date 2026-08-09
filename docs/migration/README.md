# Migration artifacts

Fixed home for the TMDB migration's reconciliation baseline (NEU-1030). The
location is pinned here rather than improvised at cutover, because milestone 5's
go/no-go re-runs the same harness against the same file.

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
