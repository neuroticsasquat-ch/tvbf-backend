# tvbf-backend

FastAPI service backing TV Binge Friend. Four subsystems today:

- **TMDB ingestion** — mirrors the TMDB catalog (shows, seasons, episodes, networks, genres, alternative titles, credits) into the local `catalog` schema via a full pass, a daily delta and a credits backfill. The TV Maze ingest it replaced was retired in NEU-1050; the `tvmaze` schema still holds its data until NEU-1051 drops it.
- **Browse API** — gated read endpoints over the mirrored catalog: search (name + AKA), filter, sort, paginate, detail.
- **User service** — invite-gated signup, password login, session cookies, account self-service.
- **Watchlist** — per-user show membership, episode-watched tracking, derived "watch next" / "upcoming" feeds.

Friend connections and the social layer land in later phases.

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
curl -sk https://api.tvbf.localhost/healthz   # -> {"status":"ok"}
task test
```

Frontend serves at `https://app.tvbf.localhost`; backend at `https://api.tvbf.localhost`. Cookies are scoped to `.tvbf.localhost` so the SPA's session cookie reaches the API.

`task -l` lists every target.

## Development

Everything runs inside the container via `task`:

| Task | Purpose |
|---|---|
| `task up` / `task down` / `task build` | container lifecycle |
| `task logs` | stream container logs (Ctrl+C to detach; container keeps running) |
| `task shell` | bash inside the container |
| `task test` | full pytest suite |
| `task test -- tests/integration/routers/test_browse.py::test_name` | run a single test or file (args forwarded) |
| `task lint` / `task format` | `ruff check` / `ruff format` |
| `task typecheck` | `pyright` |
| `task coverage` | pytest with coverage; HTML lands in `./htmlcov/` |
| `task migrate` | `alembic upgrade head` |
| `task makemigration -- "msg"` | autogenerate a new migration |

Tests are split into `tests/unit/` (pure-Python, no DB) and `tests/integration/` (real session against `tvbf_test`). Source is bind-mounted, so uvicorn's `--reload` picks up edits without a rebuild. Dependency changes (`pyproject.toml`) require `task build`.

## Health

| Endpoint | Purpose |
|---|---|
| `GET /healthz` | liveness — process is responding |
| `GET /readyz` | readiness — process is up AND can reach Postgres (`SELECT 1`). Returns `503` if the DB is unreachable. |

The Docker `healthcheck` in `docker-compose.yml` probes `/healthz`.

## Auth

Invite-gated. Signup requires a valid invite code (issued via the admin endpoints below). Login establishes a server-side session and a cookie scoped to `.tvbf.localhost`. All `/me/*` and browse endpoints require the session cookie.

| Endpoint | Purpose |
|---|---|
| `POST /auth/signup` | create account; consumes invite code |
| `POST /auth/login` | password login; sets session cookie |
| `POST /auth/logout` | end current session |
| `POST /auth/logout-all` | end every active session for the current user |

Login attempts are rate-limited via `app.login_attempt`. Sessions live in `app.session` with `expires_at`.

## Browse API

Gated by the session cookie. Every response carries `Cache-Control: public, max-age=300`. CORS is restricted to `CORS_ALLOWED_ORIGINS` (default `https://app.tvbf.localhost`).

| Endpoint | Purpose |
|---|---|
| `GET /shows` | paginated list — supports `search`, `status`, `genre` (repeatable, AND), `network` (repeatable, OR), `language`, `type`, `sort`, `page`, `per_page` |
| `GET /shows/{id}` | full show detail with embedded seasons |
| `GET /shows/{id}/seasons` | seasons array for a show |
| `GET /shows/{id}/episodes` | episodes array for a show; optional `?season=N` |
| `GET /episodes/{id}` | single episode detail |
| `GET /genres` | full genre list (flat, no pagination — ~30 rows) |
| `GET /networks` | full network list (flat, no pagination — ~400 rows) |

`search` matches the show's primary name OR any of its AKA names (token-AND). When the match is via AKA only, the response row carries `matched_aka` so the UI can show *why* a foreign-titled show came back.

Pagination is offset-based. `page` ≤ 1000, `per_page` ≤ 100. Sort keys: `name`, `-name`, `premiered`, `-premiered`, `tvmaze_updated`, `-tvmaze_updated`, `last_aired`, `-last_aired`. Episodes per show are returned in one response; no pagination on that list.

FastAPI's auto-generated API docs are at `/docs` (Swagger UI) and `/redoc`.

## /me — user-scoped state

Session-cookie-gated. All routes act on the calling user.

| Endpoint | Purpose |
|---|---|
| `GET /me` | current user profile |
| `DELETE /me` | self-delete account (cascades to sessions and watch state) |
| `POST /me/password` | change password |
| `GET /me/shows` | watchlist with derived progress + next-episode |
| `PUT /me/shows/{show_id}` | add a show to watchlist |
| `DELETE /me/shows/{show_id}` | remove a show from watchlist |
| `GET /me/watch-next` | next unwatched episode per show on the watchlist |
| `GET /me/upcoming` | upcoming episodes for shows on the watchlist |
| `GET /me/shows/{show_id}/episodes/watched` | watched-episode IDs for a show |
| `POST /me/episodes/{episode_id}/watch` | mark an episode watched |
| `DELETE /me/episodes/{episode_id}/watch` | unmark an episode |
| `POST /me/shows/{show_id}/seasons/{n}/watch` | mark every aired episode in a season |
| `DELETE /me/shows/{show_id}/seasons/{n}/watch` | unmark every episode in a season |
| `POST /me/shows/{show_id}/watch` | mark every aired episode of a show |
| `DELETE /me/shows/{show_id}/watch` | unmark every episode of a show |
| `GET /me/shows/{show_id}/season-progress` | per-season counts (watched / aired / total) |

## Admin endpoints

Guarded by `Authorization: Bearer $ADMIN_TOKEN` (default `dev-secret-change-me` in localdev — override via compose or `.env`). Server-to-server only; never exposed to the browser.

| Endpoint | Purpose |
|---|---|
| `POST /admin/catalog-ingest` | kicks off the full TMDB catalog pass as an in-process async task; returns `202 + run_id` |
| `GET /admin/catalog-ingest/{run_id}` | poll that pass's progress |
| `POST /admin/catalog-update` | run one TMDB delta by hand (in-process async) |
| `GET /admin/ingest/{run_id}` | status of a run of **any** kind — how a run with no status route of its own is polled |
| `POST /admin/invites` | create an invite code |
| `GET /admin/invites` | list every invite (consumed and unconsumed) |

Convenience wrappers: `task ingest:catalog`, `task ingest:catalog:status -- <uuid>`, `task update:catalog`, `task ingest:status -- <uuid>`.

A full catalog pass is ~229k series and takes ~8.7 hours: the loop is sequential, so latency binds at ~7.6 req/s and the 20 req/s budget never does. It is **resumable** — `catalog.show.tmdb_synced_at` is its watermark, so re-triggering only fetches what a previous attempt did not finish. The startup lifespan hook cancels any `running` run whose `last_progress_at` exceeds `INGEST_STALE_RUN_MINUTES` (default 15).

The daily delta runs as a Coolify scheduled task, `python -m tvbf.jobs.catalog_update`, whose exit code *is* the result; `HEALTHCHECK_CATALOG_URL` points at a healthchecks.io deadman for the case Coolify cannot see, which is the task never running at all.

Invite codes never expire — they consume on first use. Revoke an unredeemed invite by deleting its row.

## Database

One Postgres database, three schemas that matter here:

- `catalog` — the source-neutral catalog mirror, filled from TMDB and read by every browse, search, `/me` and credits route.
- `tvmaze` — the retired TV Maze mirror. Nothing in the running app reads it any more; it stands, with `ingest_run` and the request-budget row, until NEU-1051 drops it.
- `app` — user accounts, sessions, watch tracking, invites. Cross-schema FKs from `app.user_show_watch.show_id` and `app.user_episode_watch.episode_id` reference `catalog` with full referential integrity.

The test suite uses a separate `tvbf_test` database in the same Postgres instance; the conftest `session` fixture creates schemas and tables at session scope and truncates between tests.

## Configuration

All config flows through environment variables (read by `src/tvbf/config.py`). Full list in `docker-compose.yml`; the ones worth knowing:

| Var | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | Postgres URL | writable DB |
| `ADMIN_TOKEN` | `dev-secret-change-me` | bearer token for `/admin/*` |
| `CORS_ALLOWED_ORIGINS` | `https://app.tvbf.localhost` | comma-separated allowlist |
| `COOKIE_DOMAIN` | `.tvbf.localhost` | session-cookie scope |
| `TMDB_READ_ACCESS_TOKEN` | unset | TMDB API Read Access Token (the long JWT), sent as `Authorization: Bearer` |
| `TMDB_RATE_LIMIT_REQUESTS` / `TMDB_RATE_LIMIT_WINDOW_SECONDS` | `20` / `1` | token-bucket rate limit |
| `HEALTHCHECK_CATALOG_URL` | unset | healthchecks.io deadman for the scheduled TMDB delta |
| `INGEST_CONSECUTIVE_FAILURE_THRESHOLD` | `10` | abort a run after N consecutive per-show failures |
| `INGEST_STALE_RUN_MINUTES` | `15` | startup cleanup threshold |
| `LOG_LEVEL` | `INFO` | Python root logger level |

## Quality gates

Pre-commit hooks (`.pre-commit-config.yaml`) run `ruff check`, `ruff format --check`, `pyright`, and `pytest` — all via `docker compose exec` against the running container. Requires the container to be up:

```sh
pipx install pre-commit
pre-commit install
```
