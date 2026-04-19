# tvbf-backend

FastAPI service backing TV Binge Friend. The current scope is two subsystems:

- **TV Maze ingestion** — mirrors the TV Maze catalog (shows, seasons, episodes, networks, genres) into a local Postgres instance via an initial bulk ingest and a daily delta update.
- **Browse API** — public read-only endpoints over the mirrored catalog (search, filter, sort, paginate, detail).

User accounts, watch tracking, and friends/social land in later phases.

Stack: Python 3.13, FastAPI, SQLAlchemy 2 (async) + asyncpg, Alembic, Pydantic v2, httpx. Ruff, pyright, pytest. Packaged as a single container — no local Python required.

## Prerequisites

- Docker.
- [`go-task`](https://taskfile.dev).
- The shared `tbc-localdev-infra` stack (Postgres 17, Traefik with TLS for `*.localhost`, Mailpit) running on the external `proxy` Docker network.

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
| `task test -- tests/test_browse_routes.py::test_name` | run a single test or file (args forwarded) |
| `task lint` / `task format` | `ruff check` / `ruff format` |
| `task typecheck` | `pyright` |
| `task coverage` | pytest with coverage; HTML lands in `./htmlcov/` |
| `task migrate` | `alembic upgrade head` |
| `task makemigration -- "msg"` | autogenerate a new migration |

Source is bind-mounted into the container, so uvicorn's `--reload` picks up edits without a rebuild. Dependency changes (`pyproject.toml`) require `task build`.

## Health

| Endpoint | Purpose |
|---|---|
| `GET /healthz` | liveness — process is responding |
| `GET /readyz` | readiness — process is up AND can reach Postgres (`SELECT 1`). Returns `503` if the DB is unreachable. |

The Docker `healthcheck` in `docker-compose.yml` probes `/healthz`.

## Browse API (public, read-only)

No authentication. Every response carries `Cache-Control: public, max-age=300`. CORS is restricted to the `CORS_ALLOWED_ORIGINS` list (default `https://tvbf.localhost`).

| Endpoint | Purpose |
|---|---|
| `GET /shows` | paginated list — supports `search`, `status`, `genre` (repeatable, AND), `network` (repeatable, OR), `language`, `type`, `sort`, `page`, `per_page` |
| `GET /shows/{id}` | full show detail with embedded seasons |
| `GET /shows/{id}/seasons` | seasons array for a show |
| `GET /shows/{id}/episodes` | episodes array for a show; optional `?season=N` |
| `GET /genres` | full genre list (flat, no pagination — ~28 rows) |
| `GET /networks` | full network list (flat, no pagination — ~400 rows) |

Pagination is offset-based. `page` ≤ 1000, `per_page` ≤ 100. Sort keys: `name`, `-name`, `premiered`, `-premiered`, `tvmaze_updated`, `-tvmaze_updated`. Episodes per show are returned in one response; no pagination on that list.

FastAPI's auto-generated API docs are at `/docs` (Swagger UI) and `/redoc`.

### Examples

```sh
# Search by name substring
curl -sk "https://tvbf-backend.localhost/shows?search=breaking"

# Running dramas, sorted by most recently updated
curl -sk "https://tvbf-backend.localhost/shows?status=Running&genre=Drama&sort=-tvmaze_updated"

# Multi-genre AND: shows that are Drama AND Crime AND Mystery
curl -sk "https://tvbf-backend.localhost/shows?genre=Drama&genre=Crime&genre=Mystery"

# Show detail (id 82 = Game of Thrones)
curl -sk "https://tvbf-backend.localhost/shows/82"

# Episodes in season 3 of show 82
curl -sk "https://tvbf-backend.localhost/shows/82/episodes?season=3"
```

## Admin endpoints

Guarded by `Authorization: Bearer $ADMIN_TOKEN` (default `dev-secret-change-me` in localdev — override via compose or a `.env`).

| Endpoint | Purpose |
|---|---|
| `POST /admin/ingest` | kicks off initial bulk ingest as an in-process async task; returns `202 + run_id` |
| `GET /admin/ingest/{run_id}` | poll progress |
| `POST /admin/update` | run one daily-delta cycle synchronously |

Convenience wrappers: `task ingest`, `task ingest:status -- <uuid>`, `task update`.

A full initial ingest is ~80k shows. At the configured rate limit (~18 requests / 10 s) plus per-show upsert overhead the wall-clock time is ~12–16 hours. Runs are **resumable**: re-triggering `/admin/ingest` after an interruption only fetches shows not yet present in the database. The startup lifespan hook cancels any `running` run whose `last_progress_at` exceeds `INGEST_STALE_RUN_MINUTES` (default 15).

## Database

One Postgres database, two schemas:

- `tvmaze` — catalog mirror (owned entirely by this service). Tables: `show`, `season`, `episode`, `genre`, `network`, `web_channel`, `show_genre`, `ingest_run`.
- `app` — reserved for users, friend connections, and watch tracking (populated in a later phase; cross-schema FKs from `app.*` to `tvmaze.show.id` are the expected integration point).

The test suite uses a separate `tvbf_test` database in the same Postgres instance; the conftest `session` fixture (re-)creates schemas and tables at session scope and truncates between tests.

## Configuration

All config flows through environment variables (read by `src/tvbf/config.py`). Full list is in the `environment:` block of `docker-compose.yml`; the ones worth knowing:

| Var | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | Postgres URL | writable DB |
| `ADMIN_TOKEN` | `dev-secret-change-me` | bearer token for `/admin/*` |
| `CORS_ALLOWED_ORIGINS` | `https://tvbf.localhost` | comma-separated allowlist for browse endpoints |
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
