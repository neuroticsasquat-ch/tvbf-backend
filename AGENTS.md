# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repo layout

This is the **backend** repo for **TV Binge Friend**, a web app for tracking TV watching with a
social layer (accepted/pending/blocked connections + friend-scoped show/episode activity). It is a
FastAPI service: a TMDB catalog mirror (full pass + daily delta + airdate reconciliation), a browse
API, and a user/tracking layer (signup/login with invite codes, sessions + CSRF, My Shows,
episode/season/show watch tracking, Watch Next, Upcoming, recommendations).

Its sibling is **[`tvbf-frontend`](https://github.com/neuroticsasquat-ch/tvbf-frontend)** — a React
19 + Vite 6 + TypeScript + Tailwind v4 + shadcn/ui SPA, which carries its own `.claude/CLAUDE.md`.
Both are cloned side by side under one parent directory, alongside `tbc-localdev-infra`:

```
<parent>/
  tvbf-backend/     <- you are here
  tvbf-frontend/
  tbc-localdev-infra/
```

Nothing outside this repo is required to work in it. There is no umbrella-level `CLAUDE.md` or
`docs/` any more — everything relocated into the two repos on 2026-08-20 so a `git clone` is
sufficient on a new machine.

**Doing frontend work from a backend-rooted session.** PyCharm holds both repos in one project with
this one as the root, so a session started here loads this file and not the SPA's. If the task
touches anything under `tvbf-frontend/`, **read `../tvbf-frontend/.claude/CLAUDE.md` in full before
the first edit** — it is the same load-bearing prose this file is, not a summary, and the frontend's
patterns are no more guessable from here than these are from there. It is a pointer rather than an
`@` import on purpose: this file is already ~22k tokens and the SPA's is another ~6k, which every
backend-only session would otherwise pay for nothing. Editing across the boundary also needs the
directory added to the session — `permissions.additionalDirectories` in the gitignored
`.claude/settings.local.json`, or `/add-dir ../tvbf-frontend` for a one-off.

- `.claude/docs/` — the five split-out pattern files, listed under *Non-obvious patterns* below.
  Not optional reading; each says when you must read it.
- `docs/adr/` — architecture decision records, cited by number throughout this file.
- `docs/specs/` — approved design specs. **`specs_dir: docs`** (repo-relative). Every backend spec
  lives here, next to `docs/adr/`, so both are version-controlled, reviewable in a PR and
  permalinkable. **Cross-repo contracts live here too** — a spec the frontend repo or a later
  ticket must cite by URL is a backend-repo spec even when the work itself is shared. That rule is
  NEU-1031's and it survived the relocation; what changed is that it is no longer a *contrast*
  with anywhere else, because there is no unversioned home left to contrast against. The three
  project specs (`tvbf-*-project-spec.md`) are here for the same reason: both repos cite them.
  **Frontend-only specs live in `tvbf-frontend/docs/specs/`.** The split was made on each spec's
  own `**Repo:**` header line.
- `docs/plans/` — implementation plans, when a spec needs a separate one.
- `docs/superpowers/{specs,plans}/` — the retired layout, kept as an archive. New docs go to the
  flat paths above.
- `docs/migration/` — the TMDB migration runbook and its committed baselines.
- `docs/migration-working/` — working artifacts from migration runs (reports, probe SQL).

**Specs written before 2026-08-20 say they "live in the umbrella `docs/`".** That directory is
gone; read those sentences as "this repo's `docs/specs/`" (frontend-only ones, as
`tvbf-frontend/docs/specs/`). The rationale each one gives — cross-repo citation or its absence —
is still the rule; only the two homes it chose between were renamed. Left as written rather than
rewritten across ~10 files, because the reasoning is the part worth keeping and editing it would
restate a decision that was correct when made.

**Linear home** (for `/personal:projectit`, per `spec-and-plan-convention.md`):

- `linear_initiative: TV BingeFriend`
- `linear_team: Neuroticsasquatch`
- `linear_repos: tvbf-backend, tvbf-frontend`

The `repo:<name>` label `loop.py` filters on is derived per repo from its own git remote basename,
so it is `repo:tvbf-backend` here — the name above must match.

**Catalog freshness.** The full TMDB catalog pass runs ~8.7 hours wall-clock. Locally, `catalog.show` may hold anywhere from a few thousand to the full ~229k rows depending on how far the most recent pass got. The browse API and tests work against whatever's present; the pass is resumable (`task ingest:catalog` picks up where it left off, keyed on `tmdb_synced_at`). Check `docker exec tbc_postgresql_db psql -U root -d tvbf -tAc "SELECT COUNT(*) FROM catalog.show"` for a quick freshness check. The `tvmaze` schema is gone (NEU-1051) — `catalog` is the only spine.

## Hard constraints

- **Git is allowed here, within limits.** Branching, staging, committing and pushing are fine to do without asking, including from subagents. Follow the release workflow below: never commit directly to `main`, cut a branch first. Destructive operations — `reset --hard`, `push --force`, branch deletion, history rewriting — still need explicit confirmation every time. (This repo previously forbade all state-changing git; that rule was lifted 2026-08-01.)
- **Run `task format` before committing.** Pre-commit runs `ruff format --check`, which reports bad formatting but does not fix it — so an unformatted file fails the commit *after* you have already staged. Chain it: `task format && git add -A && git commit`.
- **There is no local Python toolchain.** Every `python`/`pytest`/`alembic`/`ruff`/`pyright`/`uv` invocation runs inside the container. Deps install into the container's system Python via `uv pip install --system .[dev]` (no `.venv`). `task` targets wrap `docker compose exec` — use them.
- **The shared localdev infra at `../tbc-localdev-infra/` must be running first.** That compose project owns `tbc_postgresql_db`, Traefik (TLS termination for `*.localhost`), Mailpit, and the external `proxy` Docker network this service joins. If Postgres or Traefik isn't up, nothing here works. Start it with `task infra:up` (namespace comes from an include in `Taskfile.yml`).

## Commands

All `task` commands run from this repo's root.

Container lifecycle:
- `task up` / `task down` / `task build` — docker compose up/down/build
- `task logs` — follow (Ctrl+C to stop, container keeps running)
- `task shell` — bash inside the container
- `task infra:up` / `task infra:down` — wraps the shared localdev infra Taskfile

Database:
- `task db:init` — create `tvbf` and `tvbf_test` databases in the shared Postgres (idempotent)
- `task migrate` — `alembic upgrade head` against `tvbf`
- `task makemigration -- "message"` — autogenerate a new migration
- `task db:refresh` — replace the local `app` schema with prod's (anonymizes users by default; `ANONYMIZE=0` opts out). Anonymisation rewrites `email`, `display_name`, `handle` and `password_hash` and truncates `app.handle_release`, preserving the first three only for `ADMIN_EMAIL`; `handle` is rewritten unconditionally for `display_name`'s reason and to the same `user_<8 hex>` shape off the same eight id characters, and unlike `display_name` a prefix collision there actually raises, at `uq_user_handle` (NEU-1163); `display_name` is rewritten **unconditionally** rather than only when it looks like an address (NEU-1195), because a conditional rule would be a second copy of NEU-1194's email-shaped test and would still leave real names in a local copy. It takes the same eight id characters the email rewrite takes, so `User 3f4a2b1c` and `user-3f4a2b1c@anon.local` are visibly one account and a prefix collision surfaces once, at `uq_user_email` — **changing the email derivation alone silently removes `display_name`'s only collision check**. The pass then asserts its own result (every non-admin row matches `^User [0-9a-f]{8}$`) and aborts, because `ON_ERROR_STOP` catches a statement that raised and not a `CASE` a later edit breaks into matching everyone. `scripts/refresh_db.sh` also takes a `catalog` mode, which replaced the `tvmaze` one in NEU-1051 and is far larger than it was; `both` does the pair. Needs `PROD_SSH` in the gitignored `.env.local`. Runs `task migrate` at the end, so migrations on your branch that prod lacks are applied on top of the restored data.
- `task db:set-password -- email [password]` — reset a local user's password (default `localdev`)

Quality gates:
- `task test` — full pytest suite
- `task test -- tests/test_browse_routes.py::test_name` — single test or file; `{{.CLI_ARGS}}` is forwarded verbatim
- `task lint` — `ruff check src tests`
- `task format` — `ruff format src tests` (in place)
- `task typecheck` — `pyright src tests`
- `task coverage` — pytest with `--cov=src/tvbf --cov-report=term-missing --cov-report=html`; HTML report lands in `htmlcov/` (volume-mounted out of the container)

Admin operations (all protected by `Authorization: Bearer $ADMIN_TOKEN`):
- `task ingest:status -- <uuid>` — `GET /admin/ingest/<uuid>`, the one status route that filters on nothing, so it reads a run of any kind. This is how a run with no status route of its own is polled (`catalog_update` today), and how the retired TV Maze runs stay readable — NEU-1051 moved the table into `catalog` with `SET SCHEMA`, so those rows outlived the schema they were written in. Its path outlived the `POST /admin/ingest` it was named for (NEU-1050); renaming it would break both for nothing.
- `task ingest:catalog` / `task ingest:catalog:status -- <uuid>` — `POST /admin/catalog-ingest`, the full TMDB catalog pass (NEU-1034). Mirrors every series in the daily id export into `catalog`, one request per show for 97.5% of them. Resumable via `catalog.show.tmdb_synced_at`. **~8.7h measured** for the ~229k series (2026-08-10, 150-show sample at 7.27 shows/sec), not the project spec's ~3.2h — same correction NEU-1065 made to enrichment, and for the same reason: the loop is sequential so latency binds at ~7.6 req/s and the 20 req/s budget never does. Safe to kill and restart. Ran after `copy:catalog` and `enrich:tmdb-ids`, never before — that order needed no special handling anywhere. Both the copy and the window are gone (NEU-1051); `docs/migration/README.md` is the account of what ran.
- `task update:catalog` — `POST /admin/catalog-update`, one TMDB catalog delta (NEU-1035). The *manual* trigger; in prod it runs as a **Coolify scheduled task**, `python -m tvbf.jobs.catalog_update`, awaiting the same body and exiting 0/1 — the process is the run, so there's no 202. The shape lives in `tvbf/jobs/scheduled.py`, shared with the TV Maze daily until NEU-1050 retired that one, and with `airdate_reconcile` since NEU-1145. Its deadman is `HEALTHCHECK_CATALOG_URL`, and the rule that put it on its own check still binds: one check fed by two tasks would let either keep it alive while the other quietly stopped. No status route of its own (a delta is minutes, not hours) — poll the unfiltered `GET /admin/ingest/{run_id}`.
- `task snapshot:trending` — `python -m tvbf.jobs.trending_snapshot`, the daily trending snapshot (NEU-1055). The *manual* trigger; in prod it is the **fourth Coolify scheduled task**, on `jobs/scheduled.py`'s run-row shape with `kind='trending_snapshot'` (added to `ck_ingest_run_kind` NOT VALID) and **its own `HEALTHCHECK_TRENDING_URL`** — never a shared one, for the reason stated at the delta, and the gap is widest here because a snapshot that stops being taken does not error, it ages out under NEU-1056's seven-day cutoff and the section simply disappears. One request a day: `/trending/tv/week`, twenty entries, resolved against `catalog.show.tmdb_id` and stored whole in `catalog.trending_show`. **`week` not `day`** — the job runs daily either way, so `day` buys volatility rather than freshness. **Deliberately not ordered after the catalog delta**, unlike the airdate pass: an entry TMDB created this morning is dropped from today's snapshot whichever runs first, and measured that case has never occurred (20 of 20 ids resolved). No status route of its own — poll it through `task ingest:status -- <uuid>`.
- `task verify:airdates:capture` / `task verify:airdates:verify` / `task verify:airdates:shows` — `python -m tvbf.jobs.airdate_verify capture|verify|shows` (NEU-1145), the proof the correction worked. **Capture the baseline before the first reconciliation pass** — `app.watch_archive` snapshotted TV Maze's airdates pre-cutover, which makes it a free labelled test set of 440 watched episodes, and AC 3's claim that the *already-correct* rows were left alone is unrecoverable after the fact. Artifacts ride stdin/stdout for the `reconcile` reasons. **`verify` inverts `reconcile verify`'s exit rule**: movement is the point here, so only a regression fails — a disagreement that grew, one that changed sign without reaching zero (the over-correction a per-network rule would have produced), or a row that stopped resolving at all. Rows still a day early are printed, not scored; some are refusals on purpose. `shows` asserts nothing — AC 2 is a human reading Apple's schedule. `docs/migration/README.md` has the runbook.
- `task reconcile:airdates` — `python -m tvbf.jobs.airdate_reconcile`, the nightly airdate reconciliation (NEU-1145). The *manual* trigger; in prod it is the **second Coolify scheduled task**, on the same `jobs/scheduled.py` shape and with its own `HEALTHCHECK_AIRDATE_URL` — never the catalog delta's, for exactly the reason stated there. Establishes one `catalog.air_date_offset` row per `(show, season)` against the TV Maze oracle, then projects it onto rows already stored. Idempotent. **Scope and due are two questions since NEU-1149**: scope is unchanged (a user tracks it, or it holds a future-dated episode), but a show in scope is reconciled only when it has never been done, when `catalog.show.tmdb_synced_at` advanced past its `catalog.airdate_show_state.last_reconciled_at`, when it is still airing, or when its sweep turn has come — `show_id % SWEEP_DAYS` with `SWEEP_DAYS = 7`, one bucket a night, plus a floor at `2 * SWEEP_DAYS`. **The sweep is what preserves the self-healing property** the old full-list-every-night behaviour gave for free; it is amortised on purpose, because a plain "last reconciled over a week ago" synchronises permanently on the first run after deploy. **Schedule it after the catalog delta in Coolify** — nothing in the repo can enforce that, and a run that starts first reads yesterday's `tmdb_synced_at` and picks a change up a night late (bounded: airing shows are nightly regardless, everything else by the sweep). It was ~3,500 requests / ~23 min against a work list of ~1,772 checkable shows every night; since NEU-1148 the oracle's show id is cached in `catalog.airdate_show_state`, so a settled run spends one request per show rather than two and takes roughly half as long — the *first* run after deploy still costs the full amount either way, because the table is created empty and nothing is backfilled, which is also why NEU-1149 needs no backfill. The opening log line breaks the night's count down by clause. `kind='airdate_reconcile'`, added to `ck_ingest_run_kind` NOT VALID, so poll it through `task ingest:status -- <uuid>`.
- `POST /admin/invites` (body `{"email_hint": "..."}`) — generate invite code; `GET /admin/invites` lists them. Codes never expire and are consumed once.

Migration operations (no auth — CLI only, run inside the container):
- `task enrich:tmdb-ids` — `python -m tvbf.jobs.tmdb_enrichment` (`-- --limit N` for a smoke run). Attaches `tmdb_id` to the copied `catalog.show` rows in three tiers, recording which one matched in `catalog.show.match_method` (NEU-1043). Idempotent; **ran after `copy:catalog` and before the TMDB ingest** — past the ingest it cannot match anything, because every series then has a row holding its `tmdb_id` and the collision guard refuses them all. Spent, and `copy:catalog` no longer exists (NEU-1051). **3h28m measured** for the full ~89k shows (2026-08-10), not the 90 min first estimated: the loop is sequential, so latency binds at ~7–8 shows/sec and the 20 req/s budget never does. Commits every 500. Exit 0 = the pass completed — unmatched shows are the expected output, not a failure, and are what NEU-1044/NEU-1045 consume. Ran in prod 2026-08-10: 62,882 matched, 26,143 unmatched, 107 collisions. Known bug, repaired by hand rather than fixed (NEU-1065) — see the module docstring.
- `task queue:list` / `task queue:confirm -- <show_id> <tmdb_id>` / `task queue:reject -- <show_id>` — `python -m tvbf.jobs.human_queue`, the human matching queue (NEU-1044). `list` reports every show **a user has touched** whose mapping nobody vouched for (`tmdb_id IS NULL`, or `match_method = 'title_year'`), as JSON on stdout, with TMDB candidates unless `-- --no-candidates`. `confirm` / `reject` write `match_method = 'human'` — with a `tmdb_id` and without one respectively — and exit 1 on any refusal. **Runs after `enrich:tmdb-ids` and before the TMDB ingest**, for the same reason enrichment does: past the ingest every series already has a row holding its `tmdb_id`, so `confirm` can only report the collision. The prod residue is 6 rows; `docs/migration/README.md` has the runbook.
- `task map:episodes` / `task map:episodes:report` — `python -m tvbf.jobs.episode_map map|report`, the episode-grain mapping (NEU-1045). `map` attaches `tmdb_id` to the copied `catalog.episode` rows of every matched show by `(season_number, episode_number)`, one request per show; `report` writes JSON to stdout listing every unmapped episode a user has watched or rated, worst first, plus the shows where *nothing* mapped. **Runs after `enrich:tmdb-ids` and before the TMDB ingest** — that ordering is what makes the ingest's `ON CONFLICT (tmdb_id)` land on the row `app.user_episode_watch` already points at instead of inserting a second one. Idempotent, resumable, and it writes nothing in `app`.
- `task dedupe:seasons` / `task dedupe:seasons:report` — `python -m tvbf.jobs.season_dedupe dedupe|report`, the season-grain deduplication (NEU-1119). The copy left every `catalog.season` with `tmdb_id IS NULL` and nothing ever mapped that grain, so the ingest inserted a second row per season on every matched show. `dedupe` re-points the episodes onto the surviving ingested row and deletes the copy, 500 seasons per transaction; `report` writes JSON to stdout and writes nothing. **Delete rather than map because the ingest has run** — 228,841 shows synced in prod, so `uq_season_tmdb_id` refuses the NEU-1045-style mapping. Prod residue of 188,134 copied seasons: 122,350 deleted, 47,445 kept under unmatched shows, 18,339 kept with no TMDB counterpart, 0 ambiguous, 2,125,419 episodes re-pointed. Idempotent, and **re-run it after any later ingest or delta**. `deletable_duplicates` reaching zero says the pass has nothing left to do, not that the grain is clean — `still_doubled` is the residue it cannot reach. `docs/migration/README.md` has the runbook. Its revert ran through `task copy:catalog`, which NEU-1051 deleted along with the `tvmaze` schema — past that ticket the pre-drop dump is the only way back.
- `task repoint:episodes` / `task repoint:episodes:report` — `python -m tvbf.jobs.episode_repoint repoint|report`, the episode-grain re-point (NEU-1126). NEU-1045's mapping pass merged a day *after* the ingest started, so its window had shut and all 7,137 watched-or-rated episodes still point at copied rows. `repoint` moves `app.user_episode_watch`, `app.user_episode_rating` and `app.activity_event` onto the ingested twin and deletes the copy; `report` writes JSON to stdout and writes nothing. **Writes to `app`**, which `season_dedupe` goes out of its way not to — it was the *only* such pass until NEU-1146's `orphan_retire` became the second, and the one that also *creates* rows there. Run `report` first. Prod residue: 1,908,478 re-pointable (6,948 user-touched), 781,266 kept with no TMDB counterpart, 889 kept because two copies share one twin, 189 user-touched kept. **Everything it kept is what NEU-1146 then retires** — that pass is the tail this one could not reach, and "kept" there meant "kept until a rule wider than the exact key exists", not "kept forever" (ADR-0012). Idempotent, and it **refuses to run before the full ingest** (`IngestNotRun` and `MIN_INGESTED_SHOWS`, which lived in `show_prune` until NEU-1051 deleted that pass and moved them here). **Ran after NEU-1046 and before NEU-1047.** `docs/migration/README.md` has the runbook, including the per-write-site revert.
- `task retire:orphans` / `task retire:orphans:report` — `python -m tvbf.jobs.orphan_retire retire|report`, the orphan-row retirement (NEU-1146). The pass that **ends** the locally-authored residue at all three grains: 782,161 episodes, 18,341 seasons and 2 shows still held TV Maze titles, airdates and numbering as of 2026-08-14, and while they are served CC BY-SA attribution is a licence condition. Four match tiers in order, each requiring 1:1 uniqueness on both sides — exact key; folded title unique within the show (**the air date is deliberately not consulted**, §2.4); a show link plus the exact key translated by a constant season offset (Will & Grace's revival is a separate TMDB series, and a title-gated rule drops `s9e1 "Eleven Years Later"` against TMDB's `"11 Years Later"`); then folded title alone inside the linked pair (Cunk on Earth, which TMDB models as a season of an anthology). Everything else is **deleted, user rows included** — ~95 watch records, which is [ADR-0012](docs/adr/0012-the-catalog-is-sole-sourced-from-tmdb.md) reversing the migration's absolute constraint and the locally-authored half of ADR-0008. **Run `report` first and commit its loss list**: it names every row that would be deleted rather than moved, split into de-duplications a surviving twin already records and genuine losses. It is also **the one pass that creates `app` rows** (a My Shows entry where history moves to a series the user does not track), so `reconcile:verify` will not come back clean afterwards, by design. Refuses to run before the full ingest, idempotent, and **re-run it after any later ingest or delta** — tier 0 found 880 rows on 2026-08-14 that had accrued since NEU-1126 ran two days earlier. Not reversible. `docs/migration/README.md` has the runbook.
- `task backfill:credits` / `task backfill:credits:report` — `python -m tvbf.jobs.credits_backfill backfill|report`, the credits backfill (NEU-1127). The full ingest finished 2026-08-11 and the two credit writers merged *later that morning*, so all 228,841 mirrored shows carry **no credits at all** — `show_cast`, `show_crew`, `episode_guest_cast`, `episode_crew` and `person` are empty tables behind read paths NEU-1047 has already repointed. `backfill` re-fetches the same payload and writes **only** the four credit tables and their three lookups, through `upsert.write_series_credits`; `report` writes JSON to stdout and nothing else. Work list is `tmdb_synced_at IS NOT NULL AND credits_synced_at IS NULL` — a **column**, not a "has no `show_cast` row" predicate, because that predicate cannot tell *upstream has none* from *nobody asked* and would never converge. ~8.7h, resumable per show, `--limit N` for a smoke run. Ran in prod 2026-08-13, which is what unblocked NEU-1051 — `tvmaze` held the only credits we had until then. `docs/migration/README.md` has the runbook.
- `task backfill:recommendations` / `task backfill:recommendations:report` — `python -m tvbf.jobs.recommendations_backfill backfill|report`, the recommendations backfill (NEU-1052). The **one-time** cost of NEU-1031 having skipped the `recommendations` namespace: all 229,418 mirrored shows predate it, so `catalog.show_recommendation` is empty. From NEU-1052 on the namespace is in `DEFAULT_APPEND`, so **every future ingest and nightly delta refreshes a show's list as a side effect** — there is no recurring pass, no cadence and no staleness rule. `backfill` re-fetches with `("recommendations",)` and nothing else appended (one request per show whatever its season count) and writes through `upsert.write_series_recommendations`; `report` writes JSON to stdout. Work list is `tmdb_synced_at IS NOT NULL AND recommendations_synced_at IS NULL` — a **column** for the reason NEU-1127's is, because upstream recommending nothing is ~8% of the long tail and the row-existence predicate cannot tell that from *nobody asked*. **Ordered `popularity DESC`, and stopping early is a supported outcome, not a failed run** — the top 20,000 take ~45 min and cover essentially every page a user loads; the whole catalog is ~8.8h. Keyset-paged on `(coalesce(popularity, -1), id)` because a failed show keeps its null watermark and a cursorless page would hand it back forever. Re-run it after any later full ingest. `docs/migration/README.md` has the runbook.
- `task archive:watches` — `python -m tvbf.jobs.watch_archive`. Snapshots every watch and rating into `app.watch_archive` (NEU-1029). Idempotent and append-only; exits 0 only when the archive covers every source row. Re-run freely.
- `task reconcile:capture` / `task reconcile:verify` — `python -m tvbf.jobs.reconcile` (NEU-1030). Per-user, per-show counts of tracked shows, watches, ratings and activity events; `verify` diffs the live DB against `docs/migration/reconciliation-baseline.json` and **exits 1 on any difference, gain or loss**. The baseline that matters is production's — see `docs/migration/README.md`.

Recommendations operations (no auth — CLI only, run inside the container):

- `task recommend:dry-run -- <user-uuid>` — `python -m tvbf.jobs.weekly_recommendations --dry-run --user <uuid>` (NEU-1105). Compiles one user's taste payload, writes it to **stdout**, reports what it holds on **stderr**, and exits **without calling a provider** — so `task recommend:dry-run -- <uuid> > payload.json` leaves a file holding exactly what the model would be sent, on `jobs/reconcile.py`'s artifact rule. **CLI-only, deliberately no admin endpoint**: the payload is a user's complete watch history, and an HTTP surface for it is a data-egress surface even behind the admin token. The **token count is an estimate and is printed as one** — there is no offline DeepSeek tokenizer here and asking the provider is what this path exists not to do, so it is bytes over `BYTES_PER_TOKEN`, NEU-1100's live measurement, which includes the instruction (`recommendations/prompt.py`) and therefore errs high. Refuses without `RECOMMENDATION_MODEL`, because the model id is *in* the hash and a payload compiled under a guessed one prints a hash no real run will match. Below the floor still exits 0 — the dry run answered the question. The bare invocation is the weekly pass itself (below).
- `task recommend:trigger` (`-- <user-uuid>` for one account) — `POST /admin/recommendations`, the HTTP trigger for the same pass (NEU-1110). Bearer `ADMIN_TOKEN`, optional `{"user_id": "..."}` body, `202 + run_id`, background task. It exists so a prompt change can be tried against one account without waiting for Sunday. **The `run_id` is a correlation id, not something to poll** — the job writes no run row, so `task ingest:status` does not read it and `task logs` is where the run is watched. It takes the same `ADVISORY_LOCK_KEY` the schedule takes, through `run_pass_if_free`, so a manual trigger during the cron logs and does nothing rather than spending a second call per user; that shared seam is why neither trigger calls `run_pass` directly. Two refusals answered synchronously, because a 202 is the only other thing it can say: **503** when `RECOMMENDATION_MODEL` / `DEEPINFRA_API_KEY` are unset, **404** for a `user_id` no account has. Unlike `--dry-run`, an HTTP surface is fine here — the endpoint returns no watch history.
- `task recommend:weekly` (`-- --user <uuid>` for one account) — `python -m tvbf.jobs.weekly_recommendations`, the weekly pass (NEU-1109). The *manual* trigger; in prod it is a **Coolify scheduled task running Sundays**, on `jobs/scheduled.py`'s NEU-1008 contract — the process is the run, the exit code is the result, `configure_logging()` in `main()`. It is the **third Coolify scheduled task**, alongside the nightly catalog delta and the airdate reconciliation, and like both it carries **its own deadman** — `HEALTHCHECK_RECOMMENDATIONS_URL` (NEU-1111) — never a check shared with them, for the reason stated there. **Schedule it Sundays, after that night's catalog delta**; nothing in the repo can enforce that, and the cost of getting it wrong is only that a show mirrored last night is not yet recommendable. The pings hang off `run()` rather than `jobs/scheduled.py` (that shape is built around a run row this job does not have) but keep its three rules: `/start` first, `/fail` on every exit-1 path, and **nothing at all when the advisory lock is held** — the pass that does hold it pings nothing itself, so a success ping would report an outcome this process never learns; silence lets the grace period expire and someone looks. **`-- --user <uuid>` pings nothing either**: a run narrowed to one account is a hand-run, and feeding the check with it would silence the deadman for the week on the strength of one user having been covered. Per user: compile the payload, compare its hash to the current set's, check the floor, call the model, resolve titles, filter exclusions, write the set and its rows. **There is no `ingest_run` row and no run table** — `app.user_recommendation_set` already *is* the per-user run record (project spec §10), so `task ingest:status` does not read this job. `pg_try_advisory_lock(ADVISORY_LOCK_KEY)` replaces `_reject_if_in_flight`, which the missing run table also removes; a second process finding it held **logs and exits 0**, because a concurrent pass is not an error. **Exit 1 if any user failed, 0 otherwise** — at 3-5 accounts one failure is 20-33% of the user base and the client has already walked its backoff curve. `insufficient_history` and `no_matches` are the pass working, not failures. `CONSECUTIVE_FAILURE_LIMIT = 3` abandons the rest. Sequential per user; the fix at 100-200 users is a bounded semaphore, not a rewrite.

## Architecture at the big-picture level

### Runtime shape

One FastAPI container joined to the `proxy` Docker network. Traefik routes `https://api.tvbf.localhost/` to it. The frontend SPA serves at `https://app.tvbf.localhost/`. The app reaches Postgres at `tbc_postgresql_db:5432` over the shared network. Uvicorn's `--reload` watches `/app/src` (bind-mounted from host). Docker `healthcheck:` pings `/healthz`.

### Endpoint surface

Path index only. **`.claude/docs/architecture-endpoints.md` is the annotated version** — read it before
adding, changing or removing a route, because most of these carry a documented reason for their cache
header, per-user fields and status codes.

- **Health:** `/healthz` (liveness), `/readyz` (readiness — `SELECT 1`, else 503).
- **Browse** — user-gated via `get_current_user` at the router level, CORS allowlisted, `Cache-Control:
  private, max-age=300` by default with per-route `private, no-store` overrides (see the cache pattern
  below): `GET /shows`, `/shows/{id}`, `/shows/{id}/seasons`, `/shows/{id}/episodes?season=N`,
  `/episodes/{id}`, `/genres`, `/networks`, `/shows/{id}/similar`, `/trending`, `/anticipated`,
  `/shows/{id}/cast`, `/shows/{id}/crew`, `/people`, `/people/{id}`, `/people/{id}/credits`,
  `/episodes/{id}/guest-cast`, `/episodes/{id}/crew`.
- **Auth** — cookie session + CSRF; session cookie is httpOnly, scoped to `.tvbf.localhost`; CSRF token
  returned in the body and required on mutating `/me/*` via `X-CSRF-Token`: `POST /signup` (invite code
  and a required `handle`), `POST /login`, `POST /logout`, `POST /password-change`.
  Since NEU-1163 signup answers `409 handle_unavailable` for a taken *or* previously released
  handle, byte-identical for both, beside the unchanged `409 email_in_use`. Signup and login also carry
  NEU-1160's IP throttle and (signup only) Turnstile, and since NEU-1162 `POST /login` refuses a
  **disabled** account with the same generic `401 invalid_credentials` a wrong password gets — see
  `.claude/docs/patterns-auth-and-abuse.md`.
- **Me** — `get_current_user` dep, mutating routes also `require_csrf`: `GET /me`,
  `PATCH /me/handle` (NEU-1163; its own route, 3 changes per 30 days, `429 rate_limited` +
  `Retry-After`, `409 handle_unavailable`), `DELETE /me`,
  `GET/PUT/DELETE /me/shows` and `/me/shows/{id}`, `GET /me/watch-next`, `GET /me/upcoming`,
  `/me/upcoming/seasons`, `/me/upcoming/shows`, `GET /me/shows/{id}/episodes/watched`,
  `GET /me/shows/{id}/seasons/progress`, `POST/DELETE /me/episodes/{id}/watched`,
  `POST/DELETE /me/shows/{id}/season/{n}/watched`, `POST/DELETE /me/shows/{id}/watched`,
  `GET /me/recommendations`, `POST /me/recommendations/{show_id}/dismiss`.
- **Reports** — `get_current_user` + `require_csrf`: `POST /reports` (NEU-1162). Always `204` once the
  row is committed, then best-effort Linear issue + maintainer email; 400 `cannot_report_self`, 404
  `reported_user_not_found`, 429 `rate_limited` at 5 per 24h per reporter. No verified-email gate.
- **Admin users** — cookie session, `require_admin_user` + `require_csrf`: `GET /admin/users`,
  `PATCH /admin/users/{user_id}/admin`, `PATCH /admin/users/{user_id}/disabled` (NEU-1162; 403
  `cannot_disable_self`, 404 `user_not_found`).
- **Admin reports** — cookie session, `require_admin_user`, no CSRF (it is a GET): `GET
  /admin/reports` (NEU-1197; paged, `?reported_user_id=` filters, and it deliberately filters
  nothing on `disabled_at`).
- **Connections** — `get_current_user` dep, mutating routes also `require_csrf`: `POST
  /connection-requests`, `GET /me/connection-requests`, `POST /connection-requests/{id}/accept`, `DELETE
  /connection-requests/{id}`, `GET /me/connections`, `DELETE /me/connections/{user_id}`, `POST/DELETE
  /me/blocks/{user_id}`, `GET /me/blocks`. Since NEU-1157 `POST /connection-requests` carries the
  per-requester budget and its **check order is load-bearing**: `429` → cooldown `409` → `404` → `400`
  → pair `409` → `201`. The throttle goes first so that at the cap every target answers an identical
  `429 rate_limited`; last, it would be a free silent oracle. Accepting, declining and cancelling are
  never throttled. See `.claude/docs/patterns-auth-and-abuse.md`.
- **Friend engagement** — `get_current_user` dep, accepted connections only (`_accepted_friend_ids`;
  pending, blocked and — since NEU-1162 — disabled excluded): `GET /shows/{show_id}/friends`,
  `GET /episodes/{episode_id}/friends/watched`.
- **Admin** — `Authorization: Bearer $ADMIN_TOKEN`: `POST /admin/catalog-ingest`, `GET
  /admin/catalog-ingest/{run_id}`, `POST /admin/catalog-update`, `GET /admin/ingest/{run_id}` (any-kind
  run status), `POST /admin/recommendations`, `POST /admin/invites`, `GET /admin/invites`. Every `POST`
  returns `202 + run_id` and runs in a background task, and **409s if a run of that same kind is already
  in flight** (`_reject_if_in_flight` → `runs.find_live_run`, scoped per kind, liveness qualified by
  `INGEST_STALE_RUN_MINUTES`) — `/admin/recommendations` is the one exception, guarded by an advisory
  lock instead. The TV Maze triggers were removed in NEU-1050.
- FastAPI auto docs at `/docs` and `/redoc`.

### Database topology

**One Postgres instance, three schemas.** It was four until NEU-1051 dropped `tvmaze` — the retired TV
Maze mirror, whose 3.5M episodes, 484k people and 2.67M guest-cast rows went with it. The pre-drop
`pg_dump` is the only copy; `scripts/dump_tvmaze.sh` takes and verifies it and
`docs/migration/README.md` has the runbook. **`.claude/docs/architecture-database.md` is
the annotated version** — every table with the reason it exists, the anonymisation rules, and the
migration-era history. Read it before adding a table or writing a migration.

- `catalog` — the source-neutral catalog mirror that replaced `tvmaze` (ADR-0007), and **the read spine
  since NEU-1047**: every browse, search, `/me` and credits route reads it. Spine `show` / `season` /
  `episode`; lookups and join tables (`genre`, `network`, `production_company`, `keyword`,
  `watch_provider`, `country`, `language`, plus one join table per relationship); show-scoped detail
  (`content_rating`, `show_aka`, `translation`, `image`, `video`, `episode_group`, `show_creator`,
  `show_recommendation`, `trending_show`); credits (`person`, `character`, `crew_role`, `show_cast`,
  `show_crew`, `episode_guest_cast`, `episode_crew`); and the operational tables `air_date_offset`,
  `airdate_show_state`, `rate_budget` and `ingest_run` (every run of every kind, moved here by NEU-1051
  with `SET SCHEMA`, so historical rows and the retired kinds in `ck_ingest_run_kind` came along).
  Built to `docs/specs/NEU-1031-tmdb-coverage-audit.md`, machine-checked by
  `tests/unit/catalog/test_audit_coverage.py`. Since NEU-1146 the spine holds no `tmdb_id IS NULL` rows
  — ADR-0012 withdrew ADR-0008's locally-authored resting state — though they stay *representable*,
  which is what protects a row mid-flight.
- `app` — user / auth / tracking / social data: `user`, `session`, `auth_token`, `login_attempt`,
  `auth_attempt`, `invite`, `user_show_watch`, `user_episode_watch`, `user_show_rating`,
  `user_episode_rating`, `connection`, `connection_request_log`, `activity_event`, `watch_archive`,
  `user_recommendation_set`, `user_recommendation`, `user_recommendation_dismissal`, `user_report`,
  `handle_release` (NEU-1163 — a released handle is never claimable by anyone but its original owner,
  and it is the handle-change throttle's ledger).
  One Alembic version table
  (`app.alembic_version`); migrations live in `migrations/versions/`.
- `import_ne` — staging tables for the one-off Next Episode data import (`series`, `episode_mark`,
  `show_candidate`, `show_match`, `show_resolution`, ...). Not part of the running app; kept for re-runs
  and auditing.

Cross-schema foreign keys are intentional and used, and since NEU-1046 they point at **`catalog`**, not `tvmaze`: `app.user_show_watch.show_id`, `app.user_show_rating.show_id`, `app.user_recommendation.show_id` (NEU-1106) and `import_ne.show_resolution.show_id` reference `catalog.show.id`; `app.user_episode_watch.episode_id` and `app.user_episode_rating.episode_id` reference `catalog.episode.id`. All with full referential integrity, and each `ON DELETE` behaviour carried across verbatim — the `app` ones all CASCADE, the `import_ne` one is NO ACTION. That was a constraint swap and not a data migration: no `app` row's values changed, because NEU-1042 preserved TV Maze's ids as the catalog surrogates. `app.activity_event` is polymorphic with no FK at all and so had nothing to repoint, which is why the reconciliation harness counts its rows explicitly.

Because `app` tables are built by `create_all` in the test suite and by Alembic in prod, every one of these constraints is **named explicitly in `app/models.py`** so the two agree. Test fixtures need `tests/fixtures/spines.py`, which since NEU-1051 holds only `without_catalog_fk(session, table)` — it stands one constraint down for a block, needed by the handful of tests that reconstruct the *pre-repoint* state the unmirrored-row reports exist to find. Its `mirror_spine` sibling went with the schema it mirrored from; tests seed `catalog` directly now.

**Anything that drops and restores the `catalog` schema must restore these FKs.** `DROP SCHEMA ... CASCADE` removes them silently, and constraints defined on tables outside the restored set do not come back with a `pg_dump --schema=catalog`. `scripts/refresh_db.sh` handles this by snapshotting `pg_get_constraintdef` for every FK pointing into the restored schemas from outside them, then replaying it after `pg_restore` — don't replace that with a hardcoded list, which is exactly how the ratings and `import_ne` constraints got silently dropped.

### Ingestion subsystem

Two entry points sharing one per-show codepath (`tmdb/ingest.py:mirror_series`). The TV Maze pair this replaced — `POST /admin/ingest` and `POST /admin/update`, with `tvmaze/{ingest,update,client,upsert,tombstone}.py` behind them — was removed in NEU-1050, and NEU-1051 removed the package entirely: `runs.py` moved to `tvbf/catalog/`, `models.py` and `catalog_copy.py` went with the schema.

- **Full catalog pass** (`POST /admin/catalog-ingest`): spawns `asyncio.create_task`, returns `202 + run_id`. Diffs TMDB's daily id export against `catalog.show.tmdb_synced_at`, fetches each missing series with `append_to_response` and its overflow seasons, and upserts each one in its own transaction. If the container crashes or is restarted mid-run, the next trigger resumes off the watermark. The startup lifespan hook marks runs whose `last_progress_at` is older than `INGEST_STALE_RUN_MINUTES` (default 15) as `cancelled`.
- **Catalog delta** (`POST /admin/catalog-update`, or the scheduled `python -m tvbf.jobs.catalog_update`): reads the last succeeded run's `last_update_cursor` as a date, walks ≤14-day windows of paged `/tv/changes`, re-fetches every changed series in full, advances the cursor, then downloads the full export once and runs both passes that read it — the popularity refresh (NEU-1172) and the tombstone reverse diff.

Per-show failures are non-fatal and increment `shows_failed`. After `N` consecutive failures (default 10, set via `INGEST_CONSECUTIVE_FAILURE_THRESHOLD`), the run aborts with `status='failed'`.

### Browse subsystem

Thin router delegating to an async SQLAlchemy query layer. No new infrastructure (no cache server, no search engine). Offset pagination with a count query per list request. Filter semantics:

- `search` — token-AND against the accent- and punctuation-folded show name **or** any of its AKAs (`catalog.show_aka.title`), as a semi-join per token rather than a JOIN, so dedupe is automatic and the count stays right without `DISTINCT`.
- `status`, `language`, `type` — exact match. Since NEU-1047 `status` carries TMDB's vocabulary (`Returning Series` / `Planned` / `Canceled` / ...) and `language` reads `show.original_language`, so its values are ISO codes (`en`) rather than names (`English`) — NEU-1037 is the frontend's half.
- `genre` (repeatable) — AND semantics, counting distinct genre **names**, in `catalog/genres.py`.
- `network` (repeatable) — OR semantics via a semi-join on `catalog.show_network`, since TMDB returns `networks[]` and a show can carry several.
- `sort` — one of eight whitelist keys (`name`, `premiered`, `tvmaze_updated`, `last_aired` + each desc variant). Unknown key → 422. `tvmaze_updated` keeps its name as a legacy alias and now orders by `coalesce(tmdb_synced_at, ingested_at)`.

The list route batch-hydrates genres and networks for all shows in a page via a single IN-query each (see `browse_queries.hydrate_show_refs`), so `GET /shows` issues 4 catalog queries total regardless of page size: count, page, genres-by-show, networks-by-show. It was five before the repoint — `web_channel` was its own query and TMDB merged the concept into `network`. Pinned by `test_get_shows_issues_a_fixed_number_of_queries_whatever_the_page_size`.

### Module map

```
src/tvbf/
  main.py              # app factory, lifespan (stale-run cleanup), CORSMiddleware, root-logger config
  config.py            # Pydantic Settings; env-driven, @lru_cache singleton
  db.py                # async engine, SessionLocal, Base (DeclarativeBase)
  deps.py              # get_session, require_admin (bearer token), get_current_user (cookie session), require_csrf
  sorting.py           # show_name_sort_key (article-stripping case-insensitive sort key)
  rate_budget.py       # per-upstream token buckets in Postgres + get_rate_limiter (ADR-0006)
  sql_fold.py          # the one accent/punctuation fold, evaluated in Postgres: folded() + folded_equal()
  client_ip.py         # the one client-address resolution: right-most X-Forwarded-For entry at N trusted hops, validated as an IP (NEU-1160)
  routers/
    health.py          # /healthz, /readyz
    auth.py            # /signup, /login, /logout, /password-change + NEU-1160's two gates: the IP throttle on the first two, Turnstile on signup
    me.py              # /me, /me/handle (NEU-1163), /me/shows, /me/watch-next, /me/upcoming, watch tracking
    browse.py          # user-gated catalog endpoints + the _set_browse_cache router dep and its _SHOW_EP_CACHE override
    admin.py           # /admin/catalog-ingest, /admin/catalog-update, /admin/ingest/{run_id} (any-kind run status)
    invites_admin.py   # /admin/invites
    admin_users.py     # /admin/users + the is_admin and disabled toggles (cookie-session admin, NEU-1162)
    admin_reports.py   # GET /admin/reports — the read-only report queue; filters nothing, no-store (NEU-1197)
    reports.py         # POST /reports — commit-then-notify, so it is always 204 (NEU-1162)
    connections.py        # /connection-requests, /me/connections, /me/blocks — the create route owns NEU-1157's check order
    friend_engagement.py  # /shows/{id}/friends, /episodes/{id}/friends/watched
  jobs/
    scheduled.py       # the shape the run-row-backed Coolify jobs share: deadman pings, per-kind guard, await-never-spawn, exit code (the weekly pass takes `ping` and the rules only — it has no run row)
    catalog_update.py  # `python -m tvbf.jobs.catalog_update` — the NEU-1035 TMDB catalog delta; exit code IS the result
    watch_archive.py   # `python -m tvbf.jobs.watch_archive` — the NEU-1029 snapshot; exit code IS the result
    reconcile.py       # `python -m tvbf.jobs.reconcile capture|verify` — the NEU-1030 cutover gate; exit code IS the verdict
    tmdb_enrichment.py # `python -m tvbf.jobs.tmdb_enrichment` — the NEU-1043 three-tier tmdb_id mapping
    human_queue.py     # `python -m tvbf.jobs.human_queue list|confirm|reject` — the NEU-1044 queue; `list` writes JSON to stdout
    episode_map.py     # `python -m tvbf.jobs.episode_map map|report` — the NEU-1045 episode grain; `report` writes JSON to stdout
    season_dedupe.py   # `python -m tvbf.jobs.season_dedupe dedupe|report` — the NEU-1119 season grain; `report` writes JSON to stdout
    episode_repoint.py # `python -m tvbf.jobs.episode_repoint repoint|report` — the NEU-1126 episode grain
    credits_backfill.py # `python -m tvbf.jobs.credits_backfill backfill|report` — the NEU-1127 credits backfill
    recommendations_backfill.py # `python -m tvbf.jobs.recommendations_backfill backfill|report` — the NEU-1052 recommendations backfill, ordered popularity DESC
    orphan_retire.py   # `python -m tvbf.jobs.orphan_retire retire|report` — the NEU-1146 orphan retirement; exit code scores criterion 7
    airdate_reconcile.py # `python -m tvbf.jobs.airdate_reconcile` — the NEU-1145 nightly airdate pass; exit code IS the result
    trending_snapshot.py # `python -m tvbf.jobs.trending_snapshot` — the NEU-1055 daily trending snapshot; exit code IS the result
    airdate_verify.py  # `python -m tvbf.jobs.airdate_verify capture|verify|shows` — the NEU-1145 proof; exit 1 only on a regression
    weekly_recommendations.py # `python -m tvbf.jobs.weekly_recommendations` — the NEU-1109 weekly pass (advisory lock, hash gate, failure semantics) + NEU-1105's `--dry-run`; exit code IS the result
  catalog/
    models.py          # SQLAlchemy tables in the catalog schema — the full TMDB surface (NEU-1032); also `ingest_run`, which every run of every kind lives in (moved here from `tvmaze`, NEU-1051)
    runs.py            # ingest_run CRUD helpers — read by the TMDB pass, the delta and the admin router
    images.py          # the one TMDB path -> URL composition + the recorded size mapping (NEU-1063, ADR-0010)
    genres.py          # the verbatim TMDB genre vocabulary + the four browse genre queries (NEU-1064, ADR-0011)
    offsets.py         # the airdate offset: the override rule, the corrected/raw pair every writer sets, the projection onto stored rows (NEU-1145)
    schemas.py         # the API's response models + ShowFilters + ALLOWED_SORT_KEYS + every catalog-row -> payload builder (NEU-1047)
    browse_queries.py  # browse/search/credits query layer (list_shows, AKA-aware search, hydrate_show_refs, ...) (NEU-1047)
    seasons.py         # the read-path season rule: one row per (show, season number), preferring the tmdb_id-bearing one (NEU-1047); SEASON_ORDER puts Specials last (NEU-1062)
    episodes.py        # what a special is (IS_SPECIAL / IS_COPIED_SPECIAL, NEU-1062) + the read-path episode ordering (NEU-1047)
  airdates/
    api_payloads.py    # the three TV Maze fields the oracle reads — the extraction is minimised at the parser (NEU-1145)
    client.py          # the TV Maze oracle: two read-only endpoints, no mirror, no credential (NEU-1145); `get_show_episodes` answers None for a 404 and [] for a show with no episodes (NEU-1148)
    show_state.py      # the cached oracle link: the three cache states, the 30-day negative expiry, and the invalidate-and-retry on a stale id (NEU-1148); also `mark_reconciled`, the NEU-1149 watermark write, because one module owns the table
    reconcile.py       # the work list + the trust rule + the pass that writes `catalog.air_date_offset` (NEU-1145); since NEU-1149 the list is scope AND due, with `SWEEP_DAYS` and the per-clause breakdown
    verify.py          # AC 2/3: the watch_archive comparison, the per-row diff and its one regression rule (NEU-1145)
  integrations/
    linear.py          # the feedback flow's three Linear GraphQL mutations
    turnstile.py       # Cloudflare siteverify: the three outcomes, read through a Pydantic envelope, fail-closed (NEU-1160)
  llm/
    api_payloads.py    # Pydantic shapes for DeepInfra's chat-completions envelope — a new upstream, so its own module (NEU-1098)
    types.py           # `Prompt`, `LLMResponse` and the three-class error taxonomy the weekly pass dispatches on (NEU-1098)
    registry.py        # the one provider and its base-URL constant; the model id is config, the base URL never is (NEU-1098)
    retry.py           # one retry policy: transient-status set, jitter, and a clamped `Retry-After` (NEU-1098)
    client.py          # `complete_json` over an OpenAI-compatible endpoint, JSON mode always on (NEU-1098)
  recommendations/
    completion.py      # per (user, show) watched/aired counts, the derived percentage and the last watch (NEU-1102)
    taste.py           # the LIKED / NOT LIKED / INTERESTED tier rules: the rating override, the dead band, the 180-day clause, and the universe those are applied over (NEU-1103)
    payload.py         # the columnar taste payload, its total row order, the `exclude` group, the regeneration hash and the weighted generation floor (NEU-1104)
    resolution.py      # the two-tier title+year -> `catalog.show` resolver: fold-exact on name, then on AKA, popularity breaking ties (NEU-1107)
    prompt.py          # the instruction, the 25-asked-for count, and the §7 output contract read back — request and response halves of one contract (NEU-1109)
    exclusion.py       # the never-recommend rule in one place: §8's four record sources plus NEU-1178's dismissal, as `show_ids_never_to_recommend` (a `Select` for the read path's anti-join) and `load_show_ids_never_to_recommend` (a `frozenset` for the payload builder) (NEU-1175, NEU-1178)
  tmdb/
    client.py          # bearer-auth httpx client + shared TMDB budget + retry + append_to_response + /find + /search/tv + /tv/changes (NEU-1028, NEU-1035)
    export.py          # the daily id export: static gzipped JSONL, no auth, no rate-budget spend, truncation-checked (NEU-1034, NEU-1036); carries `(id, popularity)` per line and owns the plausibility floors both its readers consult (NEU-1172)
    ingest.py          # full-catalog pass: export diff -> speculative append -> season overflow -> upsert (NEU-1034); owns `mirror_series`, shared with the delta
    update.py          # daily delta: cursor-as-date -> ≤14-day windows -> paged /tv/changes -> full re-fetch (NEU-1035); runs the tombstone pass at the end
    tombstone.py       # reverse diff of `catalog.show.tmdb_id` against the full export, floor-guarded (NEU-1036)
    popularity.py      # the export's other field: batched `UPDATE ... FROM (VALUES …)` onto `catalog.show.popularity`, no watermark touched (NEU-1172)
    trending.py        # `/trending/tv/week` -> `catalog.trending_show`: the whole snapshot replaced in one transaction, `captured_at` stamped at the fetch (NEU-1055)
    api_payloads.py    # Pydantic shapes for parsing the upstream TMDB API (OptionalDate/OptionalStr; no OptionalTime) (NEU-1033); nested aggregate_credits roles[]/jobs[] (NEU-1039); flat episode guest_stars[]/crew[] (NEU-1040)
    upsert.py          # TMDB payload -> catalog: conflict-target tmdb_id, episodes batched at 1000/query, season prune (NEU-1033); show cast + crew, interned character/crew_role (NEU-1039); episode guest cast + episode crew (NEU-1040)
    enrichment.py      # three-tier tmdb_id matching (tvdb_id -> imdb_id -> title+year) + match_method (NEU-1043)
    human_queue.py     # the user-touched residue: queue query + confirm/reject, both writing match_method='human' (NEU-1044)
    episode_map.py     # episode-grain mapping by (season_number, episode_number) + the unmatched report (NEU-1045)
    season_dedupe.py   # season-grain dedupe: re-point episodes onto the ingested row, then delete the copy (NEU-1119)
    episode_repoint.py # episode-grain re-point: move user history onto the ingested twin, then delete the copy (NEU-1126)
    credits_backfill.py # credits-only re-fetch for shows the ingest mirrored before the writers existed (NEU-1127)
    recommendations_backfill.py # recommendations-only re-fetch, popularity-ordered, for shows mirrored before the namespace (NEU-1052)
    orphan_retire.py   # the four-tier orphan matcher + the pass + the report, all three grains (NEU-1146)
    user_history.py    # the `app` write sites a catalog-grain retirement moves, shared by episode_repoint and orphan_retire (NEU-1146)
  app/
    handles.py         # RESERVED_HANDLES — the vendored blocklist plus this product's names and an SPA-route snapshot (NEU-1163)
    models.py          # SQLAlchemy tables in the app schema (user — carrying `handle` since NEU-1163 and `disabled_at` since NEU-1162 — session, login_attempt, auth_attempt, invite, user_show_watch, user_episode_watch, connection, connection_request_log, user_report, handle_release)
    schemas.py         # request/response models + sort literals (MyShowsSort, WatchNextSort, UpcomingSort)
    errors.py          # NotFound, AuthError, etc. — mapped to HTTP in routers
    passwords.py       # argon2 hash/verify
    tokens.py          # CSRF + session token helpers
    repos/             # one file per table; thin async query helpers (`recommendation_repo.py` spans the two recommendation *set* tables, because "the current set" is one definition, NEU-1108; `recommendation_dismissal_repo.py` is its own file for the converse reason — a dismissal is part of no set, NEU-1178; `auth_attempt_repo.py` owns the `kind` vocabulary its check constraint mirrors, NEU-1160; `user_report_repo.py` is both the report ledger and the throttle's counter, because every report is persisted anyway, NEU-1162; it also owns the admin queue's two-sided join, NEU-1197; `connection_request_log_repo.py` owns the five-outcome vocabulary its check constraint mirrors, and its `resolve` no-ops on a missing row by construction, NEU-1157)
    services/          # account_service, my_shows_service, episode_service, invite_service, connection_service, watch_archive_service, reconciliation_service, auth_throttle (the IP-keyed signup/login gate, NEU-1160), connection_throttle (the per-requester outreach budget: `current_ceiling` selects between two `Throttle`s, `enforce` counts creations, `enforce_decline_cooldown` closes the targeted case, NEU-1157), report_service (persist, commit, *then* notify — the commit boundary is the contract, NEU-1162), handle_service (the one claimability rule both write sites need, the change-and-release transaction, and the change budget, NEU-1163)

tests/
  unit/                # pure (no-DB) tests: config, sorting, deps, schema helpers, password/token, sort comparators
  integration/         # DB-backed tests; conftest seeds tvbf_test
    routers/           # ASGITransport tests for each router (auth, me, browse, admin, invites)
    app/services/      # service-level tests with seeded DB
    catalog/           # browse queries, search, genres, the read-path season rule
  fixtures/            # browse seed catalog (seeded into `catalog`) + reusable factories
```

## Non-obvious patterns

These are load-bearing and easy to get wrong.

Five areas have been split into `.claude/docs/` so this file stays inside the context limit. They are
**not optional reading** — each is the same load-bearing prose that used to sit here, and the trigger
line says when you must read it before editing.

- **`.claude/docs/patterns-auth-and-abuse.md`** — the `X-Forwarded-For` hop rule, the two complementary
  auth gates (email-keyed lockout vs. IP-keyed throttle), Turnstile's fail-closed switch, and the
  `DisplayName` email-shaped rule. Read before touching signup, login, sessions, `client_ip.py`,
  `app/services/auth_throttle.py`, `integrations/turnstile.py` or `app/schemas.py`'s `DisplayName`.
  It also owns NEU-1162's moderation surface: what `app.user.disabled_at` makes true and the one seam
  it is checked at, the three emailed-link paths that close themselves, the four invisibility
  predicates and the one deliberate exception, and `POST /reports`'s commit-then-notify contract. Read
  it before touching `app/services/report_service.py`, `app/repos/user_report_repo.py`,
  `routers/reports.py`, `routers/admin_users.py`, `routers/admin_reports.py`, or any `disabled_at` predicate.
  It also owns NEU-1163's handle rules: the normalise-don't-refuse alias, why `CITEXT` is not
  load-bearing, the `user_<8 hex>` pattern refusal, where `RESERVED_HANDLES` comes from and why the
  migration holds a second frozen copy, the 422/409 split, and `app.handle_release`'s never-reusable
  rule and doubling as the change throttle's ledger. Read it before touching `app/handles.py`,
  `app/services/handle_service.py`, `app/repos/handle_release_repo.py`, `schemas.Handle`,
  `PATCH /me/handle` or `app.user.handle`. It also owns
  NEU-1157's connection-request budget: the two ceilings and the reputation rule that selects between
  them, the three exclusions that look like oversights, the non-oracle check order, the decline
  cooldown, and the missing-row no-op every ledger writer must keep. Read it before touching
  `app/services/connection_throttle.py`, `app/repos/connection_request_log_repo.py`,
  `app/services/connection_service.py` or `POST /connection-requests`.
- **`.claude/docs/patterns-tmdb-ingest.md`** — bearer auth, the measured `append_to_response` cap of 20,
  season speculation, the `tmdb_synced_at` work list, the delta's date cursor and its five consequences,
  the floor-guarded tombstone diff, and the popularity refresh. Read before touching
  `tmdb/{client,ingest,update,tombstone,export,popularity}.py`, `POST /admin/catalog-ingest`,
  `POST /admin/catalog-update` or `tvbf.jobs.catalog_update`.
- **`.claude/docs/patterns-migration.md`** — the TV Maze → TMDB one-shot passes and their residue:
  negative special numbers, the season-list read rule, the specials ledger, `tmdb_id` mapping tiers,
  `match_method = 'human'`, season dedupe, episode mapping, the three sync watermarks, and orphan
  retirement. Read before touching any `tvbf/jobs/*` migration pass, `catalog/seasons.py`,
  `catalog/episodes.py`, or anything reasoning about copied (`tmdb_id IS NULL`) rows.
- **`.claude/docs/patterns-airdates.md`** — what an airdate now means, the offset table, the
  corrected/raw twin invariant, apply-on-write, `project_offsets`, the four grains, the TV Maze oracle's
  trust rule, and the CC BY-SA line. Read before touching `catalog/offsets.py`, `src/tvbf/airdates/`,
  `catalog.air_date_offset`, or any `air_date` / `tmdb_*_date` column.
- **`.claude/docs/patterns-recommendations.md`** — the ported `llm/` package and its accepted drift, the
  measured DeepInfra findings, the capacity screen that chose the model, the taste payload's hash and
  exclusion set, resolution's two tiers, the current-set query, the recorded fixtures, the weekly pass's
  advisory lock and retry semantics, the never-recommend rule, and dressed-title recovery. Read before
  touching `src/tvbf/llm/`, `src/tvbf/recommendations/`, `jobs/weekly_recommendations.py`,
  `RECOMMENDATION_MODEL`, `PROMPT_VERSION` or `GET /me/recommendations`.

What stays below is what applies across the whole repo regardless of which area you are in.

**Date fields use empty-string coercion.** Upstream APIs return `""` (not `null`) for unknown airdates and premiere dates. `tmdb/api_payloads.py` defines an `OptionalDate` alias wrapping `BeforeValidator(_empty_to_none)` (there is no `OptionalTime` — TMDB carries no airtime), and `airdates/api_payloads.py` restates it for the TV Maze oracle (NEU-1145) rather than importing it, on the precedent `tmdb/client.py:is_gone_upstream` sets: one alias over one validator, where an import would tie the oracle's parser to the lifetime of the mirror's. Any new upstream-payload date field must use one, not a bare `date | None`, or ingestion will fail on real data. The TV Maze *mirror* parser that established the pattern went with its client in NEU-1050.

**Vocabulary.** This repo follows the FastAPI tutorial convention:

- **models** — SQLAlchemy ORM. Lives in `models.py`.
- **schemas** — Pydantic shapes used by routes/services for *our* API. Lives in `schemas.py`.
- **api_payloads** — Pydantic shapes for parsing external upstream JSON. Carve-out so the public-API schemas don't get conflated with upstream-parser shapes. Three modules today: `tmdb/api_payloads.py` for the mirror, `airdates/api_payloads.py` for the TV Maze oracle, and `llm/api_payloads.py` for DeepInfra's chat-completions envelope (NEU-1098) — the TV Maze *mirror* parser went with its client in NEU-1050, and NEU-1145 added a much narrower one, three fields wide, because the extraction being minimal is what the CC BY-SA position rests on. **A new upstream gets its own module here**, and its date fields get an `OptionalDate`.
- **schema** in unqualified prose means a **Postgres schema** (the namespace — `catalog`, `app`) — never a Pydantic class. For the *structure-of-the-DB* sense (tables, columns, indexes), prefer "table definition" / "model + migration" / "DB shape" to avoid colliding with the namespace meaning.

**Rate limiters are cross-process, not per process, and keyed by upstream.** A budget is a token bucket row, read and decremented under `SELECT … FOR UPDATE` by `DatabaseRateLimiter` (`src/tvbf/rate_budget.py`), which is what `get_rate_limiter(source, Budget(calls, window, lease))` returns. `BUCKETS` maps a source name to the row that holds its budget — a row in `catalog.rate_budget` keyed by source (NEU-1027). Three sources are registered: TMDB, which fills the mirror; TV Maze, which NEU-1050 retired with the mirror client and NEU-1145 re-registered for the airdate oracle alone (it fills nothing); and DeepInfra (NEU-1099), which is not a catalog source at all but the model provider the weekly recommendations pass calls — a source is a ceiling to respect, not a thing we mirror. Its ceiling is **ours, not the provider's**: `DEEPINFRA_RATE_LIMIT_REQUESTS` defaults to 5/s, three orders of magnitude above a workload of one call per changed user per week, and it exists to bound the bounded-semaphore change the project spec schedules for ~100–200 users rather than to pace anything today — raising it is a measurement, not an edit. **No migration seeds its row**, and that is `catalog.rate_budget`'s own rule (migration `b1e4c7d90a52`): the limiter seeds the row at whatever capacity the caller's settings say on first use, so a seeded number would only be a second place for it to be wrong — the TV Maze re-registration added no migration either. The keying is what made each of those a row rather than a rewrite, and `tests/unit/test_rate_budget.py`'s `second_source` fixture is how the per-source properties stay asserted about the *mechanism* rather than about whichever sources happen to be live. An upstream's cap applies to us as a whole, so every job on that source splits one allowance (20 req/s for TMDB) and concurrent jobs just run slower. Constructing a bare per-client `RateLimiter` is what NEU-955 fixed; moving a delta into its own process (`python -m tvbf.jobs.catalog_update`) would have reintroduced the same doubling one level up, which is why the bucket moved into Postgres (ADR-0006). Three rules: **never sleep holding the row lock** (it turns the bucket into a serial queue and lock timeouts start reading as job failures), **use `clock_timestamp()`, never `time.monotonic()` or `now()`**, and **fail closed** when the bucket is unreachable — falling back to an in-process limiter silently restores the doubled rate exactly when nobody is watching. A fourth: `Bucket.table` and `Bucket.key_column` are interpolated into SQL, so they may only ever come from the module-level registry.

**The budget is one `Budget` argument, not three loose ones.** `get_rate_limiter` is `@cache`d, and `functools.cache` keys on the literal call — it binds no defaults and normalises no keywords, so `(20, 1.0)`, `(20, 1.0, 1)` and `(20, 1.0, lease=1)` would be three cache entries, hence three limiters holding three simultaneous leases against one row. Dataclass equality is what collapses them. Don't flatten it back out.

**`lease=` amortises lock traffic; TV Maze's `lease=1` is not an oversight.** A limiter takes `lease` tokens in one locked transaction and spends them locally, so at TMDB's 20 req/s the row sees ~1 transaction per second rather than 20 — on the component whose failure mode is lock timeouts surfacing as job failures. The block is deducted *before* any of it is spent, which is what preserves the cross-process guarantee; a process dying mid-lease forfeits the remainder, erring slow. A short grant is deliberate: a caller wanting 25 against a bucket holding 3 takes the 3 and comes back. `lease=1` is behaviour-identical to the pre-NEU-1027 limiter, which is what leaves TV Maze's calibration untouched — don't "unify" it upward.

Pass `limiter=` explicitly only to opt a caller out; `tests/conftest.py` swaps `rate_budget.build_limiter` for one returning the in-process `RateLimiter` for the whole suite, which is the only reason unit tests don't need a database.

**`app.watch_archive` is append-only, and nothing about that is by convention.** The archive (NEU-1029) is the TMDB migration's backstop: it describes what a user watched in human terms — show name, premiere year, season, episode number and title, airdate — so a mapping failure stays recoverable by hand now that `tvmaze` is gone (NEU-1051). Its writer reads `catalog`, which is both what `app` references and the only spine left. Four things hold it up.

It has **no foreign keys at all**, including none to `app.user`. `DROP SCHEMA tvmaze CASCADE` left every row standing when NEU-1051 ran it (verified, not assumed), and deleting an account leaves that user's rows too — "never pruned" has no exception, because the reconciliation harness has to count the same rows either side of cutover. The cost is real and deliberate: **a deleted user's email and display name survive here**, so this table gets dropped by hand once the migration is done. `scripts/refresh_db.sh` truncates it during anonymisation for the same reason.

The `watch_archive_no_mutation` trigger **rejects every UPDATE and DELETE** at the table. It is declared twice on purpose — in the migration, and on the model's `after_create` event so the test suite's `create_all` builds the same object. Edit one, edit the other. (TRUNCATE does not fire row triggers, which is what keeps the conftest teardown and the refresh script working.)

The writer is `INSERT ... SELECT ... ON CONFLICT DO NOTHING` with **no `DO UPDATE` branch**, so a re-run adds rows but never rewrites one — unwatch-then-rewatch keeps the original snapshot. `uq_watch_archive_source_row` is **`NULLS NOT DISTINCT`**: show-grain rows carry a NULL `source_episode_id`, and under Postgres's default they would never conflict, so every re-run would duplicate all of them. Relatedly, the placeholder columns use `cast(null(), <type>)` rather than a bare `null()` — an untyped NULL in a subquery's select list resolves to `text`, and the verification below then compares `bigint = text` and dies.

The run **verifies itself by anti-join**, counting source rows with no archive row. Not `archived >= source`: the moment a source row is deleted after being archived, the leftover row covers for a genuinely missing one and the totals balance while data is absent.

**The reconciliation harness moves its artifact on stdin/stdout, never a path.** `docs/` is not mounted into the dev container and not copied into the prod image, and a Coolify container is replaced on every deploy — so a baseline file written *inside* a container is both unreachable and short-lived. Hence `reconcile capture` writes JSON to stdout and nothing else (logs go to stderr, and the Taskfile target is `silent:` so go-task's banner cannot corrupt the artifact), and `reconcile verify` takes `--baseline -` to read stdin. That is what lets the whole flow run over `ssh 'docker exec -i ...'` against prod. Two further things are load-bearing: the joins to `{spine}.episode` are **LEFT** joins, so a watch whose episode vanished lands in a null-show bucket instead of silently leaving the count — exactly the loss the harness exists to catch; and `--spine` is interpolated into SQL, so like `rate_budget`'s `Bucket.table` it may only ever come from the module-level `SPINES` registry. One baseline spans both spines only because the migration preserves TV Maze ids as `catalog.show.id`, making `(user_id, show_id)` the same key before and after.

**Episodes upsert in batches of 1000.** Postgres caps bind parameters at 32,767 per query. A `catalog` episode row binds 15 params, so the unbatched `INSERT … VALUES (...)` fails for any show past ~2,180 episodes (soaps, daily talk, news). `tmdb/upsert.py` chunks at `_BATCH_SIZE = 1000` to stay well under the cap — and under it for every other table in that module, none of which binds more. The retired TV Maze mirror had the same ceiling at 12 params and the same fix.

**No `UNIQUE (show_id, season_number)` on `catalog.season`.** The rule was drawn on `tvmaze.season`, whose upstream occasionally returned multiple seasons with the same number for one show (a data quirk on long-running programs that catalogue seasons by calendar year), and it carried forward: a unique constraint blows up ingestion on those shows, and `season_dedupe` (NEU-1119) depends on its absence being deliberate. The primary key on `season.id` is enough; duplicates of `(show_id, season_number)` are accepted.

**`upsert_series_payload(prune_seasons=...)` is opt-in, and must stay that way.** With it True the payload is authoritative for the show's season set and seasons it doesn't name are deleted (ADR-0004) — that's what stops the mirror accruing seasons TMDB has since deleted. **Do not "simplify" this to an implicit `if not seasons: skip`.** `TMDBSeries.seasons` defaults to `[]`, so the function cannot tell "no seasons upstream" from "the caller fetched something narrower", and a delta legitimately fetches narrower; the implicit guard also conflates a legitimate zero-season show with a partial fetch, reintroducing the leak for exactly the shows where pruning matters. `prune_missing_seasons` additionally carries `tmdb_id IS NOT NULL` written out, because it deletes rather than upserts and a locally-authored row is not upstream's to take. The prune must also stay **between** the season upsert and the episode upsert — the episode writer builds its `{number: id}` map from a live query, so pruning first re-points a duplicate-numbered phantom's episodes onto the survivor instead of letting the FK null them. The retired TV Maze `upsert_show_payload` is where this rule was first drawn, on the same shape (NEU-1050).

**Shows deleted upstream are tombstoned; seasons are deleted.** The asymmetry is deliberate (ADR-0005 amends ADR-0004), and "simplifying" it to a `DELETE` destroys user data. Since NEU-1046 the FKs point at `catalog`: `app.user_show_watch` and `app.user_show_rating` cascade from `catalog.show` and `app.user_episode_watch` cascades through `catalog.episode`, so nothing upstream could restore what a delete removes. It would also fail outright — `import_ne.show_resolution` references `catalog.show` with **NO ACTION** (522 rows), raising an FK violation — and `app.activity_event` is polymorphic with **no FK at all**, so it orphans silently. A season has none of these: its only inbound FK is `episode.season_id`, `ON DELETE SET NULL`.

**The tombstone reverse diff is floor-guarded, and must stay that way.** `tmdb/tombstone.py` computes `mirrored - export`, so a truncated or empty download would tombstone the whole mirror. `_MIN_FEED_ABSOLUTE` (150,000, recalibrated for TMDB's 228,611) and the 95% relative floor are the only thing preventing that — they live in `export.py` since NEU-1172 gave them a second caller; when either trips, nothing is written **including resurrections**. The diff is also computed in Python, not as `id NOT IN (:feed)` — the export is ~229k ids against Postgres's 32,767 bind-parameter cap, the same ceiling that forces `_BATCH_SIZE`. The retired TV Maze version drew the rule first, against `/updates/shows` and a 50,000 floor (NEU-1050); the fuller TMDB-specific account is under "Tombstoning rides on the delta" below.

**No `relationship()` declarations in `models.py`.** This is deliberate — none of the code navigates via ORM relationships (it all reads via `select()` + FK columns). Test fixtures that call `session.add(Show(...))` followed by `session.add(Season(show_id=..., ...))` in one `commit()` need an explicit `await session.flush()` between them, because SQLAlchemy's unit-of-work doesn't infer FK-based insert order without `relationship()`.

**Upserts bypass the ORM identity map.** `insert(...).on_conflict_do_update(...)` is raw SQL from SQLAlchemy's perspective — it doesn't refresh ORM objects cached in the session. Tests that update a row via upsert and then re-query it need `execution_options={"populate_existing": True}` on the subsequent `select()`, or they'll get a stale identity-map hit.

**Route tests use `AsyncClient(ASGITransport(app=app))`, not `TestClient`.** Sync `TestClient` spins up its own event loop per request, which conflicts with session-scoped async fixtures (`asyncpg` raises "Future attached to a different loop"). `ASGITransport` invokes the app in-process on the pytest-asyncio session loop and avoids the problem. Both `admin_client` (in `test_admin_routes.py`) and `client` (in `test_browse_routes.py`) depend on `session` so the conftest DB teardown still runs.

**`pytest-asyncio` is configured with `fixture_loop_scope = "session"` AND `test_loop_scope = "session"`** (both in `pyproject.toml`). Do not add a custom `event_loop` fixture — that was the old pattern and conflicts with session-scoped async fixtures.

**Migrations require pre-existing schemas.** Alembic's autogenerate reads `include_name` filtering on schemas `('app', 'catalog')` but can't create them. `task db:init` creates the databases and the `app` / `catalog` schemas, so a fresh DB is just `task db:init && task migrate`. (It does not create `import_ne` — that schema is created by the Next Episode import itself.) `catalog` additionally gets a hand-written `CREATE SCHEMA IF NOT EXISTS` migration (`34a25a5fe59e`), because nothing runs `db:init` in prod and migrations are the only DDL that reaches it.

**The initial migration creates `tvmaze` even though NEU-1051 drops it.** `db:init` and CI stopped creating that schema, so on a fresh database every `schema='tvmaze'` table in the historical chain had nowhere to go and `8927c889e469` failed outright — the ticket's own third acceptance criterion. That migration therefore opens with an idempotent `CREATE SCHEMA IF NOT EXISTS tvmaze`: a virgin database creates the schema at the start of the chain and drops it at the end, and a database that already ran the migration is untouched. Editing a shipped migration is normally wrong; this is the exception, because the statement is a no-op everywhere it has already run.

**Adding a schema means editing five hand-maintained lists.** (Removing one means editing the same five — NEU-1051 is the worked example.) `migrations/env.py`'s `include_name`, the `CREATE SCHEMA IF NOT EXISTS` line in `Taskfile.yml`'s `db:init`, the *"Create databases + schemas"* step in `.github/workflows/test.yml` (CI's mirror of `db:init` — it never runs the Taskfile), `tests/conftest.py` (the session-fixture drop/create, its teardown drop, and the `schemaname IN (...)` truncate query), and this file's DB-topology section. Miss one and the suite fails in a way that looks unrelated — and the CI one fails *late*, because `conftest.py` creates the schema for `tvbf_test` itself, so only the migrate step against `tvbf` notices. `scripts/refresh_db.sh` needs no edit — its FK snapshot is generic (see below) — but a new schema only enters a refresh once it's added to that script's `RESTORED_SCHEMAS` modes.

**There is exactly one text fold and it runs in Postgres.** `src/tvbf/sql_fold.py` owns it — `folded(expr)` for a column or bind parameter, `folded_equal(session, a, b)` for two Python-side strings. It is not reimplementable in Python: `unicodedata` normalisation does not decompose ł, ø, đ or ħ, so a Python-side `unaccent` disagrees with the SQL one on precisely the titles the fold exists for. Browse search has always folded in SQL (the `ix_*_folded_trgm` expression indexes are built on this expression); NEU-1043's title matching compares titles that arrive as JSON in Python, and **binds them into the same expression** rather than growing a second definition. If you are about to write `unicodedata.normalize(...)` to compare two titles, use `folded_equal` instead.

**Every `catalog` upsert conflict-targets `tmdb_id`, never the primary key**, because the primary key is an internal surrogate `app` references and the migration seeds from TV Maze's ids (ADR-0008). Writers insert `tmdb_id` and read the surrogate back with `RETURNING`. This is also what makes a **locally-authored row (`tmdb_id IS NULL`) untouchable structurally rather than by convention**: Postgres treats NULLs as distinct in a unique index, so `ON CONFLICT (tmdb_id)` can never match one. The exception is `prune_missing_seasons`, which deletes rather than upserts and therefore needs `tmdb_id IS NOT NULL` written out — without it an authoritative payload takes a season no feed can restore. The prune otherwise ports ADR-0004 unchanged, **including staying between the season upsert and the episode upsert**, and diffs by `tmdb_id` because upstream has never heard of our ids.

**The `catalog` credit tables are not a port of the `tvmaze` ones, and three differences are measured rather than assumed** (`scripts/probe_tmdb_credit_shapes.py`, 5 series, 2026-08-11). **One `crew_role`, not two** — `tvmaze` kept `crew_role` and `episode_crew_role` apart because its vocabularies were disjoint, but TMDB emits the same `(department, job)` pair at both grains and all 78 episode-level pairs also appear at show level, so a second lookup would hold a copy of the first and split a person's credits across two tables. **No `sort_order` on either crew table** — TMDB sends no `order` on a crew entry at all (0 of 2,066 show-crew and 0 of 7,456 episode-crew entries); `episode_count` is the ordering, which is what both indexes lead on and what NEU-1039 sorts by. Cast and guest stars do carry `order` and keep the column. **`character_id` is nullable** — TV Maze always sent a character object, TMDB sends free text that is occasionally empty (1 blank of 7,629 sampled roles), and NOT NULL would abort a multi-hour pass on that one row; `uq_egc_episode_person_character` is therefore `NULLS NOT DISTINCT`, or two null-character guest credits for one person on one episode would never conflict and every re-ingest would duplicate them. Carried forward unchanged from `tvmaze`: **no** `UNIQUE (show_id, person_id, character_id)` on show cast (refresh is delete-then-insert), and the two episode uniqueness keys stay **three-part**.

**`catalog.character` is interned per show, and `catalog.person` ids are not preserved.** A character is `(show_id, name)` — TMDB has no character entity, so there is no upstream id to keep — which preserves recasting (2,621 multi-person characters in prod, all of them within one show) and loses cross-show identity (exactly one character in prod). The surrogate id survives, so the API's `CharacterRef` and the SPA need no rework. Person is the one catalog table whose identity does *not* start above TV Maze's high-water mark: the migration copies the spine because `app` references it, but credits are re-ingested from TMDB wholesale, so there is nothing to line up — the visible cost is that `/people/{id}` URLs change at cutover.

**A TMDB namespace parsed as `None` means "the caller did not append it", and every writer no-ops on it.** `[]` means upstream has none and the writer clears the table. Collapsing the two — defaulting a namespace to an empty list — would empty a show's AKAs, images, translations and providers on every narrower fetch, silently. Same shape as `prune_seasons` being opt-in, one level in.

**The backend composes TMDB image URLs, and the size mapping is a table rather than a format string.** TV Maze stored a full URL; TMDB stores a path fragment (`/abc.jpg`) that is only an image once a base URL and a size are prepended. `src/tvbf/catalog/images.py` is the one place that happens (NEU-1063, ADR-0010), which is what keeps `image_medium` / `image_original` full URLs and makes NEU-1047's "the API contract does not change" true — shipping the raw path is the more honest shape but needs a frontend ticket that does not exist. Five things are load-bearing. **`KINDS` is the record the acceptance criterion asks for** — every size `/configuration` offers per kind plus the two the API exposes (poster `w342`, still `w300`, profile `w185`, all `original`), each chosen against pixels measured off real `static.tvmaze.com` images (210x295, 250x140, 210x295) rather than TV Maze's documentation. **A size upstream does not offer is rejected both at import and on every `image_url` call**, because TMDB answers an unknown size with a placeholder image and a 200 — the one mistake nothing below the rendered page would catch, and the reason `image_url`'s `kind` argument constrains its `size` rather than merely documenting it. `medium_url` exists for `PersonRef` / `ShowRef` / `CharacterRef`, which expose `image_medium` with no `image_original` beside it, so no caller has to name a size. **An absent path is `None`, never a URL that 404s**, and never a half-present pair: a null image is now normal rather than exceptional, since NEU-1042 deliberately copied no TV Maze URLs into `poster_path`, and the SPA's fallbacks all key on the field being null. **Backdrops stay unexposed** — `backdrop_path` has no TV Maze ancestor, so exposing it is an additive contract change with no consumer; `BACKDROP` records the mapping anyway because the column is already populated. And **nothing calls the module yet**: NEU-1047 is the caller, and `CharacterRef.image_medium` becomes permanently null there, because TMDB models a character as free text and `catalog.character` has no image column.

**TMDB's genre vocabulary is adopted verbatim, and 21 of our 28 names stop existing.** Measured 2026-08-09: TMDB's `/genre/tv/list` returns 16 names against our mirror's 28, sharing only seven, so `GET /genres` returns a different list at cutover and `?genre=Anime` matches nothing (NEU-1064, ADR-0011). Mapping ours onto theirs was rejected for the audit's D1 reason — a translation layer maintained forever over a vocabulary we do not control — and because the merges are one-to-many in the wrong direction: `Sci-Fi & Fantasy` -> `Science-Fiction` would invent a claim TMDB never made. The cost is measured rather than waved at (`Romance` 12,099 shows, `Action` 5,345, `Adventure` 4,818, `Anime` 3,940) and there is no data loss behind it, because no `app` table references `catalog.genre`. Four things in `src/tvbf/catalog/genres.py` are load-bearing. **Genres come only from TMDB**, since `catalog.genre` is keyed on `tmdb_id` and NEU-1042 deliberately copied none — so a show TMDB never matched carries an empty list, which every function treats as ordinary rather than exceptional. **The AND-semantics filter counts distinct genre *names*, not genre ids**: `catalog.genre` has no `UNIQUE (name)` where `tvmaze.genre` did, and counting ids would *exclude* a show carrying two rows of the one name asked for. **Repeated values are collapsed first**, so `?genre=Comedy&genre=Comedy` sets the bar at one rather than being unsatisfiable — a small deliberate divergence from the `tvmaze` behaviour. And **nothing calls the module yet**: NEU-1047 is the caller, same boundary as `catalog/images.py`.

**CORS is locked to `CORS_ALLOWED_ORIGINS`.** Comma-separated env var, default `https://app.tvbf.localhost`. If you add a new frontend origin (e.g., production), extend the env var, don't loosen the middleware config.

**Browse's default is `Cache-Control: private, max-age=300`, and the routes carrying a per-user field override it with `private, no-store`.** The default is a *router-level dependency* — `_set_browse_cache` in `dependencies=[...]` on the `APIRouter`, not a helper each route calls — so a new browse route is cacheable unless it says otherwise, which is the wrong default for exactly one class of route and the right one for the rest. **`private` rather than `public` is deliberate and was never `public`**: browse is gated behind the session cookie, so a shared cache (CDN, corporate proxy) must not be authorized to fan one user's response out across users; the requesting browser still caches for `max-age`. **The override is `no-store` rather than merely `private`, and the reason is stronger than fan-out.** `_SHOW_EP_CACHE` covers the payloads carrying `my_rating` / `in_my_shows`, which mutate through `PUT /me/...` routes with no way to invalidate the *browser* HTTP cache — so any `max-age` lets a React Query refetch after a rating or My Shows toggle read a stale body out of the browser and silently revert the optimistic update. That is a visibly broken toggle, not a staleness nuisance, which is why the eight routes that carry such a field take it: `/shows`, `/shows/{id}`, `/shows/{id}/seasons`, `/shows/{id}/episodes`, `/episodes/{id}`, `/trending`, `/anticipated`, `/shows/{id}/similar`. The corollary binds the other way: **adding a per-user field to a route that has kept the default is a cache change, not just a payload change.** That rule has **no live example left** since NEU-1185 — `/shows/{id}/similar` was the standing one, and it spent its cacheability for the library mark (and, once spent, for `my_rating` too). What remains behind the router-level `private, max-age=300` is `/genres`, `/networks`, `/shows/{id}/cast`, `/shows/{id}/crew`, `/people`, `/people/{id}`, `/people/{id}/credits`, `/episodes/{id}/guest-cast` and `/episodes/{id}/crew` — catalog reference data with no per-user field to gain. The absence of an example is not the rule weakening: it still binds the next route that grows one, and what that route has to change is the header as well as the payload. Admin and health routes are unaffected. No ETags; defer until measured to matter.

**Type-check escape hatches.** `# type: ignore[call-arg]` on every bare `Settings(...)` construction — `src/tvbf/config.py:get_settings` plus every test that builds one (`tests/unit/test_config.py`, `tests/unit/test_email.py`, and the two NEU-1157 throttle tests; `grep -rn 'type: ignore\[call-arg\]' src/ tests/` is authoritative, on the rowcount list's precedent — this one was pinned at "two sites" and had gone stale)  (Pydantic Settings' env-driven construction confuses pyright's `Field(...)` analysis) and `# type: ignore[attr-defined]` on `result.rowcount` across fourteen files (`src/tvbf/catalog/{runs,offsets}.py`, `src/tvbf/tmdb/{upsert,enrichment,season_dedupe,episode_repoint,orphan_retire,popularity}.py`, `src/tvbf/app/services/watch_archive_service.py`, and five `src/tvbf/app/repos/*.py`; `grep -rln 'rowcount.*type: ignore' src/` is authoritative — this list has gone stale before, and had again by `show_prune`, which NEU-1051 then deleted) (SQLAlchemy's `Result` stub doesn't expose `rowcount`; only `CursorResult` does, and the runtime object is one). Preserve these when touching the files.

**Logging**: the root logger is configured in `logging_config.py:configure_logging(settings.log_level)` with `force=True` — called by `create_app` and by every `tvbf.jobs` entrypoint's `main()`. It lives outside `main.py` so a cron process doesn't build a FastAPI app and initialise Sentry just to get log formatting. All app-level `log.info/warning/exception` calls land on stderr with timestamp + level + module name, captured by Docker's json-file log driver (readable via `task logs` or `docker logs tvbf_backend`). Do not add per-module logging config.

## Planning workflow

Multi-step features follow the spec-then-plan flow already used in this repo:

1. Design spec lives at `docs/specs/<TICKET-ID>-<slug>.md`.
2. Implementation plan lives at `docs/plans/<TICKET-ID>-<slug>.md` and references its spec. Plans use TDD-style step-by-step tasks with full code in each step. Plans in this repo explicitly do NOT include `git commit` steps — the user commits on their own cadence.

Read a recent spec (e.g. `docs/specs/NEU-1197-admin-report-queue.md` or
`docs/specs/NEU-1145-airdates-one-day-early.md`) to see the expected structure before starting a
new feature. `docs/superpowers/{specs,plans}/` holds the retired layout — an archive of the
2026-04 to 2026-08 pairs, still worth reading for history, but not where new docs go.

## Pre-commit hooks

Configured in `.pre-commit-config.yaml`: `ruff check`, `ruff format --check`, `pyright`, `pytest` — all via `docker compose exec -T tvbf-backend`. Requires the container to be running for commits to succeed. Install once (on the host) with `pipx install pre-commit && pre-commit install`. (The frontend repo has no pre-commit hooks; its gates run via `task lint` / `task typecheck` / `task test` and CI.)
