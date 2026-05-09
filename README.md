# tvbf-backend

FastAPI service backing TV Binge Friend. Four subsystems today:

- **TV Maze ingestion** — mirrors the TV Maze catalog (shows, seasons, episodes, networks, genres, AKAs) into a local Postgres instance via an initial bulk ingest, a daily delta update, and a separate AKA backfill.
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
| `POST /admin/ingest` | kicks off initial bulk ingest as an in-process async task; returns `202 + run_id` |
| `GET /admin/ingest/{run_id}` | poll progress |
| `POST /admin/update` | run one daily-delta cycle (in-process async) |
| `POST /admin/backfill-akas` | backfill `show_aka` rows for shows where `akas_synced_at IS NULL`; returns `202 + run_id` |
| `GET /admin/backfill-akas/{run_id}` | poll AKA backfill progress |
| `POST /admin/invites` | create an invite code |
| `GET /admin/invites` | list every invite (consumed and unconsumed) |

Convenience wrappers: `task ingest`, `task ingest:status -- <uuid>`, `task update`, `task akas:backfill`, `task akas:backfill:status -- <uuid>`.

A full initial ingest is ~80k shows. At the configured rate limit (~18 requests / 10 s) plus per-show upsert overhead, wall-clock time is ~12–16 hours. AKA backfill takes the same order of time. Both runs are **resumable**: re-triggering only fetches shows not yet present (or shows whose `akas_synced_at` is NULL). The startup lifespan hook cancels any `running` run whose `last_progress_at` exceeds `INGEST_STALE_RUN_MINUTES` (default 15).

Invite codes never expire — they consume on first use. Revoke an unredeemed invite by deleting its row.

## Database

One Postgres database, two schemas:

- `tvmaze` — catalog mirror (owned entirely by this service). Tables: `show`, `season`, `episode`, `genre`, `network`, `web_channel`, `show_genre`, `show_aka`, `ingest_run`.
- `app` — user accounts, sessions, watch tracking, invites. Tables: `user`, `session`, `user_show_watch`, `user_episode_watch`, `login_attempt`, `invite`. Cross-schema FKs from `app.user_show_watch.show_id` and `app.user_episode_watch.episode_id` reference `tvmaze` with full referential integrity.

The test suite uses a separate `tvbf_test` database in the same Postgres instance; the conftest `session` fixture creates schemas and tables at session scope and truncates between tests.

## Configuration

All config flows through environment variables (read by `src/tvbf/config.py`). Full list in `docker-compose.yml`; the ones worth knowing:

| Var | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | Postgres URL | writable DB |
| `ADMIN_TOKEN` | `dev-secret-change-me` | bearer token for `/admin/*` |
| `CORS_ALLOWED_ORIGINS` | `https://app.tvbf.localhost` | comma-separated allowlist |
| `COOKIE_DOMAIN` | `.tvbf.localhost` | session-cookie scope |
| `TVMAZE_RATE_LIMIT_REQUESTS` / `TVMAZE_RATE_LIMIT_WINDOW_SECONDS` | `18` / `10` | token-bucket rate limit |
| `INGEST_CONSECUTIVE_FAILURE_THRESHOLD` | `10` | abort a run after N consecutive per-show failures |
| `INGEST_STALE_RUN_MINUTES` | `15` | startup cleanup threshold |
| `LOG_LEVEL` | `INFO` | Python root logger level |

## Quality gates

Pre-commit hooks (`.pre-commit-config.yaml`) run `ruff check`, `ruff format --check`, `pyright`, and `pytest` — all via `docker compose exec` against the running container. Requires the container to be up:

```sh
pipx install pre-commit
pre-commit install
```
