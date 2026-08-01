# Next Episode import

One-off tooling to import a [Next Episode](https://next-episode.net) data export
into a TVBF account: My Shows membership plus watched episodes. Linear NEU-927.

This is **not** a general importer. There is no standard export format across
TV-tracking apps, so this is throwaway operational tooling — no endpoint, no UI,
no migration. The only part worth reusing for a different export is `apply.sql`,
which takes resolved ids and knows nothing about Next Episode's format.

## How it works

The pipeline lives in an `import_ne` staging schema in Postgres. Each stage
leaves its results in a table, so you can inspect anything in psql between
steps, and the human review step is just a CSV round trip.

```
parse_export.py   export JSON      ->  import_ne.series, import_ne.episode_mark
match.sql         staging          ->  show_candidate, show_match, show_resolution
disambiguate.sql  watch marks      ->  resolves ambiguity nobody needs to see
<human review>    review.csv       ->  show_resolution (source='manual')
match_episodes…   show_resolution  ->  episode_match
apply.sql         episode_match    ->  app.user_show_watch, app.user_episode_watch
```

Only `apply.sql` writes outside `import_ne`.

`parse_export.py` runs on the host with stdlib only — no venv, no deps, and it
never touches the database. Everything else is SQL over `docker exec`. The
backend container is not needed at any point.

## Usage

```bash
./run.sh stage ~/Downloads/export.json.html   # parse, load, match, disambiguate
./run.sh review                               # -> review.csv for the leftovers
#   ... fill in chosen_show_id, one row per title ...
./run.sh resolve review.csv                   # load picks, re-match episodes
./run.sh status                               # where things stand
./run.sh apply <user_id> <now|airdate>           # dry run
./run.sh apply <user_id> <now|airdate> --commit  # write
```

Point at a different database with `LOCAL_PG_CONTAINER` / `LOCAL_DB` /
`LOCAL_DB_USER`. Every subcommand echoes its target to stderr before doing
anything.

## Running against prod

Prod is Coolify-managed, so container names carry random suffixes and must be
discovered rather than hard-coded. The pipeline is deterministic given the same
export plus `manual_resolutions.sql`, so the right move is to **re-run it against
prod** rather than trying to copy the `import_ne` schema across. Show ids are TV
Maze ids, which are global — the manual picks port unchanged.

Set `REMOTE_SSH` and it runs from your laptop — stdin is forwarded over SSH, so
every `.sql` file works unchanged and nothing needs copying to the host. Nothing
here needs the backend container or a Python venv either way.

```bash
set -a; source ../../.env.local; set +a   # PROD_SSH

# 1. find the prod postgres container (same discovery refresh_db.sh uses;
#    Coolify names are random, so never hard-code them)
ssh "$PROD_SSH" "docker ps --filter ancestor=postgres:18-alpine --format '{{.ID}}'"
ssh "$PROD_SSH" "docker exec <id> printenv POSTGRES_USER POSTGRES_DB"

# 2. confirm the target user BY EMAIL, and check the id matches
ssh "$PROD_SSH" "docker exec -i <id> psql -U <user> -d <db> -c \
  \"SELECT id, email, display_name FROM app.\\\"user\\\" WHERE email ILIKE '%briggs%';\""

# 3. run the pipeline against prod
export REMOTE_SSH="$PROD_SSH" LOCAL_PG_CONTAINER=<id> LOCAL_DB=<db> LOCAL_DB_USER=<user>
./run.sh stage ~/Downloads/briggsjm.json.html

# manual_resolutions.sql goes over the same transport
ssh "$REMOTE_SSH" "docker exec -i $LOCAL_PG_CONTAINER psql -U $LOCAL_DB_USER -d $LOCAL_DB \
  -v ON_ERROR_STOP=1" < manual_resolutions.sql
./run.sh status          # re-read this: prod's catalog differs from local

# 4. dry run, read the TARGET USER block, then commit
./run.sh apply <user_id> airdate
./run.sh apply <user_id> airdate --commit
```

**Re-run matching against prod; do not copy `import_ne` across.** Prod's catalog
is larger than any local restore, so a title that was unambiguous locally may
have gained a same-named candidate and will drop into the review pile. That
fails safe — an unambiguous match cannot silently become a *different*
unambiguous match — but the resolved count will not necessarily agree with the
local run, and `status` has to be re-read before applying.

**Expect far more collisions in prod.** A local restore is a point-in-time
snapshot; the account has kept tracking since. On the briggsjm run, local had 52
existing watches and prod had 1,691. Those are all real events, and `DO NOTHING`
is what protects them — see the invariant above.

### On the user id

Local app data is an anonymized restore of prod, and `refresh_db.sh` rewrites
**only** `email` and `password_hash` — `id` is never touched. The anonymized
address is literally built from the id (`'user-' || substring(id::text,1,8)`),
so `user-d9a07ae4@anon.local` is id `d9a07ae4-…`. The local id is therefore the
prod id.

Confirm it anyway. Look the user up in prod by email, check exactly one row comes
back, and check the id matches before passing it. `apply.sql` will refuse to run
against an id that does not exist and prints the target's email, display name and
signup date first — read that block before typing `--commit`. A wrong-but-valid
uuid is the one failure mode nothing else here would catch.

## Matching

The only show identifier in the export is a title; the only episode identifier
is title + season + episode number. Three passes, in order:

1. **Strict.** Exact case-insensitive name match, constrained by premiere year
   when the title carries a `(YYYY)` suffix. Trailing country tags (`(US)`,
   `(UK)`) are stripped too.
2. **AKA fallback**, but *only* for titles with zero strict hits. Letting AKAs
   compete with strict matches measurably makes things worse — on the briggsjm
   export it cuts no-matches from 4.8% to 1.1% but pushes ambiguity from 9.4%
   to 13.4%. It is a fallback, not a peer.
3. **Watch-mark coverage.** For titles that are still ambiguous, check which
   candidate actually contains the episodes the user claims to have watched.
   Only one *Friends* ran ten seasons. A candidate wins only if it covers
   strictly more marks than every rival, so ties and zero-mark titles are left
   for a human.

That third pass is what makes the review tractable. On the briggsjm export it
took stranded episode marks from 807 down to 45 — nearly all remaining review
is about My Shows membership, not watch history.

## Two things to get right

**Do not route writes through the service layer.** `app/services/episode_service.py`
calls `activity_service.emit()` at three sites. Thousands of backdated watches
through that path would fan out to every accepted connection's feed, all stamped
at import time, every show at once. `apply.sql` writes SQL directly, so no
`activity_event` rows can be produced at all — prevented by construction, not by
remembering a flag. It still verifies afterwards.

**`watched_at` is invented either way.** The column is `NOT NULL DEFAULT now()`
and the export contains no dates at all, only watched yes/no. `now` stamps
everything at import time, which makes recency ordering meaningless but never
claims a date before the episode aired. `airdate` keeps ordering roughly sane
but asserts a date the user never supplied. Pick deliberately.

**Tracked data outranks imported data.** A row the user created by ticking an
episode in the app is a real event, with a real timestamp. A row this script
creates is a backfill carrying a fabricated one. Every `ON CONFLICT` in
`apply.sql` is `DO NOTHING` for that reason: where the two collide, the real
event wins and the import yields. Do not change those to `DO UPDATE`.

A consequence, and an intended one: some watch timestamps will not match their
episode's airdate, because they came from actual tracking. That is not drift to
be tidied up. "Normalizing" it would overwrite real history with an invented
one, and nothing records what was there before.

## Known lossage

- **Unnumbered specials.** ~6% of marks look like `48xSpecial` — a season, but
  nothing identifying an episode. Not deterministically mappable; reported as
  `special-unnumbered` and skipped.
- **Numbering drift.** A handful of marks name episodes that do not exist on the
  matched show, because Next Episode and TV Maze count specials and two-parters
  differently. Reported as `no-such-episode`.
- **Abandoned titles.** Anything left unresolved is silently missing data from
  the user's point of view. `run.sh status` lists them — keep that list so it can
  be reported rather than discovered.
