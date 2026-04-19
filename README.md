# tvbf-backend

FastAPI service backing TV Binge Friend. Current scope: mirror the TV Maze catalog (shows, seasons, episodes, networks, genres) into a local Postgres instance via an initial bulk ingest and a daily delta update.

Stack: Python 3.13, FastAPI, SQLAlchemy 2 (async) + asyncpg, Alembic, Pydantic v2, httpx. Ruff, pyright, pytest. Packaged as a single container — no local Python required.

## Prerequisites

- Docker.
- [`go-task`](https://taskfile.dev).
- The shared localdev infra at `../tbc-localdev-infra/` (Postgres 17, Traefik with TLS for `*.localhost`, Mailpit) running on the external `proxy` Docker network.

## Quick start

```sh
# Bring up the shared infra if it isn't already.
task infra:up

# Build, start, create databases, run migrations.
task build
task up
task db:init          # creates tvbf and tvbf_test databases (idempotent)
task migrate          # alembic upgrade head

# Verify.
curl -sk https://tvbf-backend.localhost/healthz   # -> {"status":"ok"}
task test
```

`task -l` lists every target.

## Development

Everything runs inside the container via `task`:

| Task | Purpose |
|---|---|
| `task up` / `task down` / `task build` | container lifecycle |
| `task logs` | stream container logs (Ctrl+C to detach; container keeps running) |
| `task shell` | bash inside the container |
| `task test` | full pytest suite |
| `task test -- tests/test_ingest.py::test_name` | run a single test or file (args forwarded) |
| `task lint` / `task format` | `ruff check` / `ruff format` |
| `task typecheck` | `pyright` |
| `task coverage` | pytest with coverage; HTML lands in `./htmlcov/` |
| `task migrate` | `alembic upgrade head` |
| `task makemigration -- "msg"` | autogenerate a new migration |

Source is bind-mounted into the container, so uvicorn's `--reload` picks up edits without a rebuild. Dependency changes (`pyproject.toml`) require `task build`.

## Admin endpoints

Guarded by `Authorization: Bearer $ADMIN_TOKEN` (default `dev-secret-change-me` in localdev — override via compose or a `.env`).

| Endpoint | Purpose |
|---|---|
| `POST /admin/ingest` | kicks off initial bulk ingest as an in-process async task; returns `202 + run_id` |
| `GET /admin/ingest/{run_id}` | poll progress |
| `POST /admin/update` | run one daily-delta cycle synchronously |

Convenience wrappers: `task ingest`, `task ingest:status -- <uuid>`, `task update`.

A full initial ingest is ~80k shows at the rate limit (~6–8 hours). Runs are **resumable**: re-triggering `/admin/ingest` after an interruption only fetches shows not yet present in the database. The startup lifespan hook cancels any `running` run whose `last_progress_at` exceeds `INGEST_STALE_RUN_MINUTES` (default 15).

## Database

One Postgres database, two schemas:

- `tvmaze` — catalog mirror (owned entirely by this service).
- `app` — reserved for users, friend connections, and watch tracking (populated in a later phase; cross-schema FKs from `app.*` to `tvmaze.show.id` are the expected integration point).

The test suite uses a separate `tvbf_test` database in the same Postgres instance; the conftest `session` fixture (re-)creates schemas and tables at session scope and truncates between tests.

## Configuration

All config flows through environment variables (read by `src/tvbf/config.py`). Full list of keys is in the `environment:` block of `docker-compose.yml`; the ones worth knowing:

| Var | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | Postgres URL | writable DB |
| `ADMIN_TOKEN` | `dev-secret-change-me` | bearer token for `/admin/*` |
| `TVMAZE_RATE_LIMIT_REQUESTS` / `TVMAZE_RATE_LIMIT_WINDOW_SECONDS` | `18` / `10` | token-bucket rate limit |
| `INGEST_CONSECUTIVE_FAILURE_THRESHOLD` | `10` | abort a run after N consecutive per-show failures |
| `INGEST_STALE_RUN_MINUTES` | `15` | startup cleanup threshold |
| `LOG_LEVEL` | `INFO` | Python root logger level |

## Quality gates

Pre-commit hooks (`.pre-commit-config.yaml`) run `ruff check`, `ruff format --check`, `pyright`, and `pytest` — all via `docker compose exec` against the running container. Requires the container to be up. Install after `git init`:

```sh
pipx install pre-commit
pre-commit install
```

## Documentation

- Design spec: `../docs/superpowers/specs/2026-04-19-tvmaze-ingestion-design.md`
- Implementation plan: `../docs/superpowers/plans/2026-04-19-tvmaze-ingestion.md`
