# NEU-1158 — Drop watch_archive PII before open registration

**Ticket:** [NEU-1158](https://linear.app/neuroticsasquatch/issue/NEU-1158/drop-watch-archive-pii-before-open-registration)
**Repo:** `tvbf-backend`
**Project:** TVBF: Open Registration · Milestone "3. Launch switch"
**Status:** approved for implementation

---

## 1. What this is for

`app.watch_archive` (NEU-1029) was the TMDB migration's backstop: it snapshotted every watch and rating in human-readable form — show name, season, episode title, airdate — so a catastrophic mapping failure could be recovered from a plain description. It retained the email address and display name of every user, including **deleted** ones. This was deliberate and documented:

> "It has **no foreign keys at all**, including none to `app.user`. […] deleting an account leaves that user's rows too — 'never pruned' has no exception, because the reconciliation harness has to count the same rows either side of cutover. The cost is real and deliberate: **a deleted user's email and display name survive here**, so this table gets dropped by hand once the migration is done."

The migration is done. NEU-1146 (`orphan_retire`) ran on 2026-08-14 and was the last pass that needed the archive as a backstop. NEU-1155/NEU-1170 will publish a privacy policy describing `DELETE /me` as a real deletion right — shipping that page while a table quietly retains PII from deleted accounts would be a claim we know to be false at the moment we make it. This is the only ticket in the Open Registration project with genuine legal exposure, and also the cheapest to close.

## 2. Why dropping is safe

Four code paths read the archive. Each is spent:

| Reader | What it does | Status |
| -- | -- | -- |
| `jobs/watch_archive.py` | The writer — snapshots source tables into the archive | Being deleted |
| `airdates/verify.py` + `jobs/airdate_verify.py` | One-shot NEU-1145 acceptance proof: captures a baseline against production before the first airdate reconciliation, then verifies afterwards | Being deleted — baselines are committed artifacts at `docs/migration/neu-1145-airdate-baseline.json` |
| `tmdb/orphan_retire.py` | References `app.watch_archive` only in a docstring and a log message (prose, not SQL) | Pass is Done; prose updated to point at the pre-drop dump |
| `scripts/refresh_db.sh` | Truncates the table during anonymisation | TRUNCATE line removed |

**Not read at runtime.** No admin endpoint, no browse route, no user-facing page reads this table. No foreign keys point into it (deliberately — NEU-1029's design).

## 3. Scope

### In scope

- A pre-drop `pg_dump` of the table from production, verified via test-restore, matching the `scripts/dump_tvmaze.sh` precedent
- Migration that drops `app.watch_archive` and its trigger function, gated on `TVBF_WATCH_ARCHIVE_DUMP_VERIFIED=yes`
- Removal of the model, service, CLI entry point, and all tests that depend on them
- Removal of the airdate-verify mechanism (entirely spent one-shot NEU-1145 proof)
- Removal of the `task archive:watches` and `task verify:airdates:*` Taskfile targets
- Removal of the archive from `scripts/refresh_db.sh`'s anonymisation TRUNCATE and its comment block
- Update of `docs/migration/README.md`, `docs/adr/0012`, and `.claude/CLAUDE.md` to reflect the removal
- Update of quotes in `src/tvbf/jobs/orphan_retire.py` to point at the pre-drop dump

### Out of scope

- `NEU-1195` — the `display_name` anonymisation in `refresh_db.sh` that also touches the adjacent TRUNCATE block (already Done)
- The `app.user_recommendation_set` TRUNCATE in `refresh_db.sh` — that table stays (non-PII compiled payload, truncated for different reasons)
- Any shipped spec (`NEU-1145`, `NEU-1146`, `NEU-1157`, `NEU-1178`, `NEU-1195`, project specs) — historical records, left as-is
- Committed baseline artifacts (`docs/migration/neu-1145-airdate-baseline.json`, `neu-1145-airdate-shows-before.json`) — historical records, left as-is
- Any change to the `airdates/reconcile.py` nightly reconciliation or `airdates/client.py` oracle — these are live production code that do not depend on the archive

## 4. Acceptance criteria

1. **Pre-drop dump taken and verified.** `scripts/dump_watch_archive.sh` dumps `app.watch_archive` from production via `pg_dump --table=app.watch_archive`, test-restores into a throwaway database, and verifies the row count matches. The dump artifact is stored outside the database.
2. **Old migrations still run.** The table-creation migration (`c9f2b7a41d38`) stays in history; the new migration refers to it by revision ID. `alembic upgrade head` on a fresh database runs the full chain including creation and then the drop.
3. **Model and service removed.** `WatchArchive`, `watch_archive_record_type_enum`, the trigger function/trigger, and `after_create` listeners are removed from `src/tvbf/app/models.py`. `watch_archive_service.py` is deleted. `jobs/watch_archive.py` is deleted.
4. **`airdates/verify.py` and `jobs/airdate_verify.py` removed.** The entire one-shot NEU-1145 acceptance proof mechanism is deleted, including its three Taskfile targets.
5. **`scripts/refresh_db.sh` no longer references `app.watch_archive`.** The TRUNCATE list (line 355) and its explanatory comment block (lines 330-334) are removed.
6. **`Taskfile.yml` no longer references the archive or airdate-verify targets.** `archive:watches` and `verify:airdates:*` targets are removed.
7. **Docs updated.** `.claude/CLAUDE.md`, `AGENTS.md`, `.claude/docs/architecture-database.md`, `docs/adr/0012`, and `docs/migration/README.md` reflect the removal. Prose references in code that pointed at the archive as the recovery path (orphan_retire.py) now point at the pre-drop dump.
8. **`task test` green.** The full pytest suite passes.
9. **`task lint` and `task typecheck` green.**

## 5. Migration design

One new migration, hand-written (not autogenerated):

```
revision: <uuid>
down_revision: d5a91c4e2f68   # current head (add_user_handle)
```

**Statements (in order):**

1. `DROP TABLE IF EXISTS app.watch_archive CASCADE`
2. `DROP FUNCTION IF EXISTS app.watch_archive_no_mutation`
3. `DROP TYPE IF EXISTS app.watch_archive_record_type`

**Guard:** `upgrade` raises `RuntimeError("watch_archive dump not verified")` when a `SELECT count(*) FROM app.watch_archive` returns > 0 rows and `os.environ.get("TVBF_WATCH_ARCHIVE_DUMP_VERIFIED") != "yes"`. The guard stands down on an empty table, so fresh databases and CI never see it. Pattern matches `a7e3c8d15f42` (the `tvmaze` drop).

The table-creation migration (`c9f2b7a41d38`) stays in the migration chain; `alembic upgrade head` creates it and then drops it on a fresh database.

## 6. Files to delete

| Path | Reason |
| -- | -- |
| `src/tvbf/app/services/watch_archive_service.py` | The service that writes the archive |
| `src/tvbf/jobs/watch_archive.py` | The CLI entry point for `task archive:watches` |
| `src/tvbf/airdates/verify.py` | The archive-dependent capture/verify logic (NEU-1145 proof) |
| `src/tvbf/jobs/airdate_verify.py` | The CLI entry point for the proof |
| `tests/integration/app/services/test_watch_archive_service.py` | Tests for the deleted service |
| `tests/integration/airdates/test_verify.py` | Tests for the deleted verify module |

## 7. Files to create

| Path | Purpose |
| -- | -- |
| `scripts/dump_watch_archive.sh` | Pre-drop dump script. Mirrors `dump_tvmaze.sh` but targets `--table=app.watch_archive` instead of `--schema=tvmaze`. Dumps on prod, test-restores into a throwaway database, verifies row count, fetches once. |
| `migrations/versions/<uuid>_drop_app_watch_archive.py` | The drop migration (see §5). |

## 8. Files to edit

### Code

| File | Change |
| -- | -- |
| `src/tvbf/app/models.py` | Remove `watch_archive_record_type_enum`, `WatchArchive` class, `WATCH_ARCHIVE_NO_MUTATION_FUNCTION`, `WATCH_ARCHIVE_NO_MUTATION_TRIGGER`, and both `after_create` event listeners |
| `src/tvbf/jobs/orphan_retire.py:31` | Docstring: `app.watch_archive` → "the pre-drop pg_dump (NEU-1158)" |
| `src/tvbf/jobs/orphan_retire.py:103` | Log message: `app.watch_archive` → "the pre-drop pg_dump (NEU-1158)" |

### Tests (comment-only updates)

| File | Change |
| -- | -- |
| `tests/integration/catalog/test_catalog_credit_tables.py:247` | Update comment referencing `uq_watch_archive_source_row` |
| `tests/integration/tmdb/test_orphan_retire.py:579` | Update comment referencing `app.watch_archive` |
| `tests/integration/app/test_recommendation_models.py:171` | Update comment referencing `watch_archive` |

### Scripts

| File | Change |
| -- | -- |
| `scripts/refresh_db.sh` | Remove `app.watch_archive` from TRUNCATE list (line 355) and its explanatory comment block (lines ~330-334) |

### Taskfile

| File | Line(s) | Change |
| -- | -- | -- |
| `Taskfile.yml` | 175-178 | Remove `archive:watches` target |
| `Taskfile.yml` | 322-339 | Remove `verify:airdates:capture` target |
| `Taskfile.yml` | 341-351 | Remove `verify:airdates:verify` target |
| `Taskfile.yml` | 353-359 | Remove `verify:airdates:shows` target |

### Docs

| File | Change |
| -- | -- |
| `.claude/CLAUDE.md` + `AGENTS.md` | Remove command ref lines (111, 124), table list entry (221), module map entries (285, 314), service list entry (358), the "app.watch_archive is append-only" pattern block (437-445), escape-hatch list entry (491) |
| `.claude/docs/architecture-database.md` | Remove `watch_archive` from `app` schema description |
| `docs/adr/0012-the-catalog-is-sole-sourced-from-tmdb.md:52` | Update recovery path from `app.watch_archive` to the pre-drop dump |
| `docs/migration/README.md:25` | Mark `task archive:watches` row as retired |
| `docs/migration/README.md:134` | Update to note the table has been dropped |
| `docs/migration/README.md:578` | Update recovery path from `app.watch_archive` to the pre-drop dump |
| `docs/migration/README.md:1714-1715` | Update to note the baseline was taken from the now-dropped table |

## 9. Deploy sequence

1. **Run `scripts/dump_watch_archive.sh` against production** — captures the pre-drop dump
2. **Copy the dump and its counts artifact off the VM** (dump lives outside the database)
3. **Set `TVBF_WATCH_ARCHIVE_DUMP_VERIFIED=yes` in Coolify**
4. **Merge this PR** — Coolify applies migrations on deploy; the migration refuses without the env var
5. **Unset `TVBF_WATCH_ARCHIVE_DUMP_VERIFIED` in Coolify** — the variable has done its job
6. **Verify `SELECT count(*) FROM app.watch_archive` returns `ERROR: relation does not exist`** in production
7. NEU-1155/NEU-1170 can now claim deletion is complete in the privacy policy
