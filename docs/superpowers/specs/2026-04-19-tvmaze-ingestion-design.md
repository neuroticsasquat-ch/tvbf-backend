# TV Maze Ingestion & Update Service — Design

**Date:** 2026-04-19
**Status:** Approved (brainstorming)
**Scope:** Backend subsystem responsible for mirroring TV Maze show/episode data into a local Postgres database. User accounts, friend connections, watch tracking, and the React frontend are explicitly out of scope for this spec and will be designed separately.

## Context

TV Binge Friend is a web app for tracking television watching with a social layer (friends, friend activity). The backend is a FastAPI application containerized for both localdev (shared `tbc-localdev-infra` Postgres container) and production (Azure Container Apps). This spec covers the first deliverable: a service that clones the subset of TV Maze needed to power tracking and browsing, and keeps that clone fresh via a daily delta update.

TV Maze is a free, keyless HTTP API. There is no downloadable dump. The rate limit is approximately 20 calls per 10 seconds per IP. A full crawl of every show (~80k+) takes roughly 6–8 hours of API time.

## Goals

1. Ingest the full TV Maze catalog (scoped entities below) into Postgres as a one-time initial operation.
2. Apply a daily delta that keeps the local mirror consistent with TV Maze within one day.
3. Expose the trigger surface as HTTP endpoints so orchestration can live outside the app (Azure Container Apps Scheduled Job, Airflow, or equivalent — not decided here).
4. Fit cleanly into the existing localdev infrastructure (`../tbc-localdev-infra/`, shared Postgres on the `proxy` network).

## Non-goals

- User authentication, authorization, or any user-facing data model.
- Cast, crew, characters, alternate episode lists.
- Self-hosted poster/image storage. Images are served directly from `static.tvmaze.com` URLs stored in the database; local caching is a future spec.
- A production-grade secret rotation or RBAC story. Admin endpoints use a shared-secret bearer token until the user-auth phase lands.
- The React frontend.
- Recommendation or similar-shows features.

## Decisions

### D1. Entity scope: minimum tracking set

The mirror covers shows, seasons, episodes, genres, networks, and web channels. This supports "I watched S3E4," browsing by genre, season-level UI (posters, air-date ranges, episode counts), and correct per-season network attribution for shows that change networks mid-run. Images are stored as URLs (TV Maze's CDN) inside show and season rows — no separate image table and no local image caching in v1; a self-hosted image pipeline is a deliberate future spec. Cast/crew/characters are deferred; they can be added as a second ingestion pass later without modifying existing code.

Seasons are fetched as a co-embed on the same `/shows/{id}?embed[]=episodes&embed[]=seasons` request used for episodes, so adding them costs no additional API calls.

### D2. Execution model: in-process async task

`POST /admin/ingest` spawns `asyncio.create_task` inside the FastAPI container and returns `202` with a run id. `POST /admin/update` runs synchronously in the request (daily delta is short). No worker container, no Redis, no Celery. Resumability is built into TV Maze's API shape: `/updates/shows` returns every show's last-modified timestamp, so resuming an interrupted ingest is a simple diff against rows already present. A container restart mid-ingest is recoverable by re-triggering the endpoint.

### D3. Database topology: one database, two schemas

One Postgres database, two schemas: `tvmaze` (mirror) and `app` (future user-facing data). Cross-schema foreign keys are supported by Postgres and will be relied on when the user schema arrives (e.g., `app.user_show_watch.show_id REFERENCES tvmaze.show.id`). One Alembic config manages both schemas. This was chosen over dual-DB because TV Maze's schema is owned by our ingestion code (no external read-only source to isolate), and Azure backups are per-server — two-DB-on-one-server provides no backup-policy isolation, and two servers double cost for no clear benefit.

### D4. Orchestration: external, HTTP-triggered

Nothing inside the app schedules itself. Localdev triggers via a `task` target that curls the endpoint. Production uses an Azure-native trigger (Container Apps Scheduled Job, Airflow, or equivalent — deferred) that hits the same endpoint with a shared-secret bearer token. The endpoint is the single source of truth for "do one update cycle."

### D5. Dependencies: Docker-only, no local venv

`pyproject.toml` is the source of truth for dependencies. The Dockerfile installs them into the container's system Python via `uv pip install --system .`. There is no `.venv` in the repo and no local Python install required. Source code is bind-mounted for reload during development; dependency changes require an image rebuild.

### D6. Task runner: go-task wrapping `docker compose exec`

All common operations are exposed as `task` targets that wrap container execution. The root Taskfile includes the localdev infra Taskfile under the `infra:` namespace, matching the established project convention.

## Architecture

A single FastAPI application container. Routers:

- `/admin/*` — operator-only surface (ingest, update, progress polling), guarded by a shared-secret bearer token from `ADMIN_TOKEN`.
- `/healthz`, `/readyz` — liveness and readiness.
- Public/frontend routes — scaffolded as an empty router, populated in later specs.

Stack:

- Python 3.13
- FastAPI
- SQLAlchemy 2.x (async) + asyncpg
- Alembic
- Pydantic v2
- httpx (async) for the TV Maze client
- Ruff (lint + format), pytest, respx (httpx mocking)
- uv for dependency resolution inside the Dockerfile

## Module layout

```
tvbf-backend/
  pyproject.toml
  Dockerfile
  docker-compose.yml
  Taskfile.yml                # includes ../tbc-localdev-infra/Taskfile.yml under infra:
  alembic.ini
  migrations/
    env.py
    versions/
  src/tvbf/
    main.py                   # FastAPI app factory, router wiring, lifespan
    config.py                 # Pydantic Settings (DATABASE_URL, ADMIN_TOKEN, TVMAZE_BASE_URL, rate-limit knobs)
    db.py                     # async engine, session factory, declarative bases per schema
    deps.py                   # FastAPI dependencies: db session, admin-auth guard
    routers/
      admin.py                # POST /admin/ingest, POST /admin/update, GET /admin/ingest/{id}
      health.py               # /healthz, /readyz
    tvmaze/
      client.py               # async httpx client with rate limiting and retry
      models.py               # SQLAlchemy models bound to the `tvmaze` schema
      schemas.py              # Pydantic shapes matching the TV Maze API payloads
      ingest.py               # initial-bulk orchestration
      update.py               # daily-delta orchestration
      upsert.py               # idempotent per-show upsert (shared by ingest and update)
    app/
      models.py               # placeholder; the `app` schema is created empty
  tests/
    conftest.py
    test_tvmaze_client.py
    test_upsert.py
    test_ingest.py
    test_update.py
    test_admin_routes.py
```

## Data model

All timestamps are `timestamptz` in UTC. Primary keys mirror TV Maze's native integer IDs where possible so round-trips with the API do not require lookup tables.

### `tvmaze.show`

| Column | Type | Notes |
|---|---|---|
| `id` | `integer` PK | TV Maze show id |
| `name` | `text` | |
| `type` | `text` | e.g. "Scripted", "Reality" |
| `language` | `text` | |
| `status` | `text` | "Running", "Ended", etc. |
| `runtime` | `integer` nullable | minutes |
| `premiered` | `date` nullable | |
| `ended` | `date` nullable | |
| `official_site` | `text` nullable | |
| `summary` | `text` nullable | HTML; stored as-is |
| `image_medium` | `text` nullable | URL |
| `image_original` | `text` nullable | URL |
| `externals_imdb` | `text` nullable | |
| `externals_tvdb` | `integer` nullable | |
| `externals_tvrage` | `integer` nullable | |
| `network_id` | `integer` nullable FK → `tvmaze.network.id` | |
| `web_channel_id` | `integer` nullable FK → `tvmaze.web_channel.id` | |
| `tvmaze_updated` | `bigint` | epoch from TV Maze's `updated` field — the resume cursor |
| `ingested_at` | `timestamptz` default `now()` | |

### `tvmaze.season`

| Column | Type | Notes |
|---|---|---|
| `id` | `integer` PK | TV Maze season id |
| `show_id` | `integer` FK → `tvmaze.show.id` ON DELETE CASCADE | |
| `number` | `integer` | season number as used in episode lists |
| `name` | `text` nullable | |
| `episode_order` | `integer` nullable | total episodes expected for the season |
| `premiere_date` | `date` nullable | |
| `end_date` | `date` nullable | |
| `network_id` | `integer` nullable FK → `tvmaze.network.id` | season-level network (may differ from show's) |
| `web_channel_id` | `integer` nullable FK → `tvmaze.web_channel.id` | |
| `image_medium` | `text` nullable | URL |
| `image_original` | `text` nullable | URL |
| `summary` | `text` nullable | HTML |

Unique `(show_id, number)`.

### `tvmaze.episode`

| Column | Type | Notes |
|---|---|---|
| `id` | `integer` PK | TV Maze episode id |
| `show_id` | `integer` FK → `tvmaze.show.id` ON DELETE CASCADE | |
| `season_id` | `integer` nullable FK → `tvmaze.season.id` ON DELETE SET NULL | resolved from `(show_id, season)` during upsert |
| `season` | `integer` | season number — retained for direct indexing and as the pre-resolution source of truth |
| `number` | `integer` nullable | null for specials |
| `name` | `text` nullable | |
| `airdate` | `date` nullable | |
| `airtime` | `time` nullable | |
| `runtime` | `integer` nullable | |
| `summary` | `text` nullable | |
| `image_medium` | `text` nullable | |
| `image_original` | `text` nullable | |

Index on `(show_id, season, number)`.

### `tvmaze.network` and `tvmaze.web_channel`

Same shape:

| Column | Type |
|---|---|
| `id` | `integer` PK (TV Maze id) |
| `name` | `text` |
| `country_code` | `text` nullable |
| `country_name` | `text` nullable |
| `timezone` | `text` nullable |

### `tvmaze.genre`

Genres arrive as string arrays on show payloads. We deduplicate into a table with a surrogate key.

| Column | Type |
|---|---|
| `id` | `serial` PK |
| `name` | `text` unique |

### `tvmaze.show_genre`

| Column | Type |
|---|---|
| `show_id` | `integer` FK → `tvmaze.show.id` ON DELETE CASCADE |
| `genre_id` | `integer` FK → `tvmaze.genre.id` |

Composite PK `(show_id, genre_id)`.

### `tvmaze.ingest_run`

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` PK default `gen_random_uuid()` | |
| `kind` | `text` CHECK in `('initial', 'update')` | |
| `status` | `text` CHECK in `('running', 'succeeded', 'failed', 'cancelled')` | |
| `started_at` | `timestamptz` default `now()` | |
| `finished_at` | `timestamptz` nullable | |
| `last_update_cursor` | `bigint` nullable | max TV Maze epoch successfully processed |
| `shows_processed` | `integer` default 0 | |
| `shows_failed` | `integer` default 0 | |
| `last_progress_at` | `timestamptz` nullable | updated each time `shows_processed` or `shows_failed` increments; used to detect dangling `running` rows after a crash |
| `error` | `text` nullable | populated on `failed` |

## Data flow

### Initial ingestion (`POST /admin/ingest`)

1. Insert an `ingest_run` row with `kind='initial'`, `status='running'`.
2. Spawn an `asyncio.create_task` bound to the app's lifespan; the endpoint returns `202 {"run_id": ...}`.
3. The ingest coroutine:
   a. `GET /updates/shows` — returns a `{show_id: epoch}` map of every show TV Maze knows about.
   b. `SELECT id FROM tvmaze.show` — determine what is already ingested.
   c. For each show id in the diff (missing locally), acquire a rate-limit slot, then `GET /shows/{id}?embed[]=episodes&embed[]=seasons`.
   d. In one transaction per show: upsert network, upsert web_channel, upsert show, upsert genres + show_genre rows, upsert seasons (including any season-level network / web_channel), upsert episodes with `season_id` resolved from `(show_id, episode.season)` against the just-upserted seasons. Use `INSERT ... ON CONFLICT (id) DO UPDATE` throughout.
   e. Increment `shows_processed`; on per-show exception, log and increment `shows_failed` and continue.
4. On successful completion: set `status='succeeded'`, `finished_at=now()`, `last_update_cursor` = max epoch from the updates map.

Re-calling `POST /admin/ingest` while a run is in flight returns `409 Conflict` with the in-flight run id. Re-calling after a crash reuses the "shows not in DB" diff and continues where it left off — the run is effectively resumable without dedicated checkpoint code.

### Daily update (`POST /admin/update`)

1. Read `last_update_cursor` from the most recent successful run (of either kind).
2. `GET /updates/shows`; select show ids whose epoch is greater than the cursor.
3. For each such id: run the same per-show fetch-and-upsert used by initial ingest.
4. Advance the cursor to the max epoch processed; mark the run succeeded.

The update path reuses the per-show routine from the initial ingest — one code path, two entry points.

### Progress polling (`GET /admin/ingest/{id}`)

Returns the `ingest_run` row as JSON: status, counts, timestamps, error. No streaming; polling is sufficient for operator use.

## Rate limiting and retry

The TV Maze client in `tvmaze/client.py` enforces a configurable rate limit. Default: 18 requests per 10 seconds, sliding window, implemented as an asyncio token bucket. A lower limit than TV Maze's documented 20/10s provides headroom for 429s and clock skew.

Retry policy per request:

- **Transient network errors (connection reset, timeout, 5xx)**: exponential backoff, up to 5 attempts.
- **429 Too Many Requests**: honor `Retry-After` if present; otherwise exponential backoff. Does not count against the retry budget.
- **4xx other than 429**: no retry; propagate as a per-show failure.
- **Persistent failure for a single show**: caught at the orchestration layer, logged with show id and error, `shows_failed` incremented, run continues.

## Error handling and recovery

- **Per-show failure**: never kills the run. Failed show ids are logged (structured) so they can be replayed manually.
- **Container restart mid-ingest**: the in-memory task is lost. The `ingest_run` row for the interrupted run remains in `status='running'`. At FastAPI startup, a lifespan hook scans for dangling `running` rows whose `last_progress_at` is older than a configurable staleness threshold (default 15 minutes) and marks them `status='cancelled'` with an explanatory note. The operator re-triggers `POST /admin/ingest`; the diff-based resumption picks up the remaining shows.
- **Database errors during upsert**: rollback the offending show's transaction, log, increment `shows_failed`, continue.
- **TV Maze API fundamentally down**: after N consecutive 5xxs across different shows, abort the run with `status='failed'` and `error` populated. Threshold and window configurable; default N=10.

The staleness threshold for the startup cleanup is configurable via `INGEST_STALE_RUN_MINUTES` (default 15).

## Security

- `/admin/*` routes require `Authorization: Bearer $ADMIN_TOKEN`. The token is read from the `ADMIN_TOKEN` environment variable (Container Apps secret in production, `.env` in localdev). Missing or wrong token returns `401`.
- `/healthz` and `/readyz` are unauthenticated.
- No other authentication surface exists in this spec; real user auth arrives in the next phase.
- TV Maze is called over HTTPS.

## Configuration

`Settings` (Pydantic) reads from environment:

- `DATABASE_URL` — e.g. `postgresql+asyncpg://root:root@tbc_postgresql_db:5432/tvbf`
- `ADMIN_TOKEN` — shared secret for `/admin/*`
- `TVMAZE_BASE_URL` — default `https://api.tvmaze.com`
- `TVMAZE_RATE_LIMIT_REQUESTS` — default 18
- `TVMAZE_RATE_LIMIT_WINDOW_SECONDS` — default 10
- `TVMAZE_RETRY_MAX_ATTEMPTS` — default 5
- `INGEST_CONSECUTIVE_FAILURE_THRESHOLD` — default 10
- `INGEST_STALE_RUN_MINUTES` — default 15
- `LOG_LEVEL` — default INFO

## Testing

All tests run inside the backend container via `task test`.

- **Unit:** `test_upsert.py` exercises the per-show upsert against a throwaway test schema (pytest fixture creates `tvmaze_test`, runs migrations, drops at teardown), including `season_id` resolution from `(show_id, episode.season)` and season-level network/web_channel attribution. `test_tvmaze_client.py` covers rate limiting and retry behavior using respx to mock httpx.
- **Integration:** `test_ingest.py` and `test_update.py` use a small canned fixture of TV Maze payloads to drive the full orchestration end-to-end, asserting row counts, FK wiring, and cursor advancement.
- **API:** `test_admin_routes.py` covers the auth guard (401 on missing/wrong token), the `202` response shape, and progress polling.

## Deployment and containerization

### Localdev

`tvbf-backend/docker-compose.yml`:

- Service `tvbf-backend` on the external `proxy` network.
- Traefik labels routing `tvbf-backend.localhost` via TLS.
- `DATABASE_URL` points at `tbc_postgresql_db` over the shared network.
- Source bind-mounted at `/app/src` for reload; deps baked into the image.
- Dev command: `uvicorn tvbf.main:app --host 0.0.0.0 --port 8000 --reload`.

Database bootstrap: a dedicated `tvbf` database is created inside the shared Postgres instance via a one-shot `task db:init` target that runs `psql` against the shared `tbc_postgresql_db` container and issues `CREATE DATABASE tvbf` (idempotent — checks `pg_database` first). Alembic migrations then create both schemas and all tables inside `tvbf`.

### Azure (future work)

- Container image built by GitHub Actions, pushed to ACR.
- Deployed to Azure Container Apps.
- Postgres Flexible Server; credentials in Container Apps secrets.
- `ADMIN_TOKEN` and `DATABASE_URL` wired as secret-backed env vars.
- Migration step runs as a pre-deploy job (`alembic upgrade head` in the same image with a different entrypoint) before the new revision becomes active.
- Scheduled update trigger (Container Apps Scheduled Job or Airflow) decided separately. This spec only guarantees the HTTP contract it calls.

### Taskfile

Root `Taskfile.yml` includes `../tbc-localdev-infra/Taskfile.yml` under the `infra:` namespace. Top-level targets (partial list):

```
task up                            # docker compose up -d
task down                          # docker compose down
task build                         # docker compose build
task logs                          # docker compose logs -f tvbf-backend
task shell                         # docker compose exec tvbf-backend bash
task db:init                       # create the tvbf database in the shared Postgres (idempotent)
task migrate                       # alembic upgrade head
task makemigration -- "msg"        # alembic revision --autogenerate -m "msg"
task test                          # pytest
task lint                          # ruff check
task format                        # ruff format
task ingest                        # curl POST /admin/ingest with $ADMIN_TOKEN
task update                        # curl POST /admin/update with $ADMIN_TOKEN
task deps:add -- <package>         # edit pyproject + rebuild
```

All `task` targets that run Python code invoke it through `docker compose exec` so no local Python is needed.

## Open questions (deferred)

- **Production scheduler choice** (Azure Container Apps Scheduled Job vs Airflow vs something else). The endpoint contract is stable regardless.
- **Multi-stage Dockerfile** to ship a leaner prod image without dev dependencies. V1 uses a single-stage image for simplicity.
- **Cast/crew ingestion.** Scoped out of v1; will be added as a second ingestion pass when needed for recommendations or richer show detail views.
- **Self-hosted poster pipeline.** Its own future spec. Will introduce object storage (MinIO locally, Azure Blob in prod), a fetcher that populates it from the URLs already stored in `tvmaze.show` and `tvmaze.season`, and a serving strategy. No schema changes required in this spec to keep that path open.

## What comes next

After this spec is approved and implemented, the next phase addresses user accounts, authn/authz, show tracking, and friend connections — populating the `app` schema and exercising cross-schema foreign keys to `tvmaze.show`. The React frontend follows that. Recommendation and similar-shows features remain aspirational.
