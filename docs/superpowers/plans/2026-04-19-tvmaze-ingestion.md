# TV Maze Ingestion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the FastAPI backend subsystem that mirrors TV Maze's show/season/episode catalog into a local Postgres database, with one-time initial ingestion and a daily delta update, both exposed as admin-authenticated HTTP endpoints.

**Architecture:** Single FastAPI container. Initial ingest runs as an in-process `asyncio.create_task`; daily update runs synchronously. One Postgres database with two schemas (`tvmaze` for the mirror, `app` for future user data). TV Maze client is rate-limited via an asyncio token bucket. Resumability is provided by TV Maze's `/updates/shows` endpoint diffed against locally-present show ids.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy 2.x async + asyncpg, Alembic, Pydantic v2, httpx, uv (system Python in container, no local venv), Ruff, pytest, respx. Containerized via Docker Compose on the shared `tbc-localdev-infra` `proxy` network. go-task wraps operations.

**Spec reference:** `docs/superpowers/specs/2026-04-19-tvmaze-ingestion-design.md`

---

## Execution notes for the implementing engineer

- **The user handles all git operations.** After each task, stop and let the user decide when to commit. There is no `git add`/`git commit` step in this plan. Do not create branches, commit, push, rebase, checkout, or reset at any point. Read-only git commands (`status`, `log`, `diff`) are fine if you need them.
- **Everything runs inside the container.** There is no local venv and no local Python. Every `pytest`, `alembic`, `ruff`, and `curl` invocation in this plan runs via `docker compose exec` (or via `task` targets that wrap it). The Dockerfile installs all dependencies — including dev — into the container's system Python via `uv pip install --system .[dev]`.
- **Shared Postgres.** The localdev infra at `../tbc-localdev-infra/` must be running first (`task -d ../tbc-localdev-infra up`, or via the root Taskfile's `infra:up` include). The backend connects to `tbc_postgresql_db` over the external `proxy` Docker network.
- **Two Postgres databases are used locally:** `tvbf` for normal operation, `tvbf_test` for the test suite. Both are created inside the single shared `tbc_postgresql_db` container via `task db:init`.
- **Follow TDD where there is testable logic** (client, upsert, orchestration, auth). For pure infrastructure steps (Dockerfile, compose, Taskfile) verification is via running the container and observing expected output.

---

## File map

Files created or modified, in the order tasks produce them:

```
tvbf-backend/
  pyproject.toml                      # Task 1
  Dockerfile                          # Task 2
  .dockerignore                       # Task 2
  docker-compose.yml                  # Task 2
  Taskfile.yml                        # Task 3 (grows throughout)
  .env.example                        # Task 5
  alembic.ini                         # Task 8
  migrations/env.py                   # Task 8
  migrations/script.py.mako           # Task 8
  migrations/versions/                # Task 10, plus any future revision
  src/tvbf/__init__.py                # Task 1
  src/tvbf/main.py                    # Task 1 (grows in Tasks 19, 23)
  src/tvbf/config.py                  # Task 5
  src/tvbf/db.py                      # Task 6
  src/tvbf/deps.py                    # Task 22
  src/tvbf/routers/__init__.py        # Task 1
  src/tvbf/routers/health.py          # Task 1
  src/tvbf/routers/admin.py           # Task 23
  src/tvbf/tvmaze/__init__.py         # Task 9
  src/tvbf/tvmaze/models.py           # Task 9
  src/tvbf/tvmaze/schemas.py          # Task 11
  src/tvbf/tvmaze/client.py           # Task 12
  src/tvbf/tvmaze/upsert.py           # Tasks 13–17
  src/tvbf/tvmaze/runs.py             # Task 18
  src/tvbf/tvmaze/ingest.py           # Task 20
  src/tvbf/tvmaze/update.py           # Task 21
  src/tvbf/app/__init__.py            # Task 9
  src/tvbf/app/models.py              # Task 9 (placeholder)
  tests/conftest.py                   # Task 6 (grows in Task 9)
  tests/test_health.py                # Task 1
  tests/test_config.py                # Task 5
  tests/test_tvmaze_client.py         # Task 12
  tests/test_upsert.py                # Tasks 13–17
  tests/test_runs.py                  # Task 18
  tests/test_startup_cleanup.py       # Task 19
  tests/test_ingest.py                # Task 20
  tests/test_update.py                # Task 21
  tests/test_auth.py                  # Task 22
  tests/test_admin_routes.py          # Task 23
  tests/fixtures/tvmaze/              # Task 20 (canned payloads)
```

One file = one responsibility. `upsert.py` grows across five tasks but stays under 300 lines; if it exceeds that, split per entity at the end.

---

### Task 1: Python project scaffolding + minimal FastAPI app

**Files:**
- Create: `tvbf-backend/pyproject.toml`
- Create: `tvbf-backend/src/tvbf/__init__.py`
- Create: `tvbf-backend/src/tvbf/main.py`
- Create: `tvbf-backend/src/tvbf/routers/__init__.py`
- Create: `tvbf-backend/src/tvbf/routers/health.py`
- Create: `tvbf-backend/tests/__init__.py`
- Create: `tvbf-backend/tests/test_health.py`

- [ ] **Step 1: Write pyproject.toml**

```toml
[project]
name = "tvbf"
version = "0.1.0"
description = "TV Binge Friend backend"
requires-python = ">=3.13"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.32",
    "sqlalchemy[asyncio]>=2.0.36",
    "asyncpg>=0.30",
    "alembic>=1.14",
    "pydantic>=2.10",
    "pydantic-settings>=2.7",
    "httpx>=0.28",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.3",
    "pytest-asyncio>=0.25",
    "respx>=0.22",
    "ruff>=0.9",
    "httpx>=0.28",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/tvbf"]

[tool.ruff]
line-length = 100
target-version = "py313"

[tool.ruff.lint]
select = ["E", "F", "W", "I", "B", "UP"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "session"
testpaths = ["tests"]
```

- [ ] **Step 2: Write the failing smoke test for /healthz**

Create `tests/test_health.py`:

```python
from fastapi.testclient import TestClient
from tvbf.main import app


def test_healthz_returns_200_and_ok_body():
    client = TestClient(app)
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
```

- [ ] **Step 3: Implement the minimal FastAPI app**

Create `src/tvbf/__init__.py` (empty).

Create `src/tvbf/routers/__init__.py` (empty).

Create `src/tvbf/routers/health.py`:

```python
from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz() -> dict[str, str]:
    return {"status": "ok"}
```

Create `src/tvbf/main.py`:

```python
from fastapi import FastAPI

from tvbf.routers import health


def create_app() -> FastAPI:
    app = FastAPI(title="tvbf-backend")
    app.include_router(health.router)
    return app


app = create_app()
```

- [ ] **Step 4: Leave verification for Task 4**

This task's test runs inside the container; it will be executed after the container boots in Task 4. Do not attempt to run `pytest` on the host — there is no host Python install.

- [ ] **Step 5: Task complete**

The pyproject.toml and app skeleton are in place. Stop here and move on to Task 2.

---

### Task 2: Dockerfile and docker-compose

**Files:**
- Create: `tvbf-backend/Dockerfile`
- Create: `tvbf-backend/.dockerignore`
- Create: `tvbf-backend/docker-compose.yml`

- [ ] **Step 1: Write the Dockerfile**

```dockerfile
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

RUN pip install --no-cache-dir uv

WORKDIR /app

COPY pyproject.toml ./
RUN uv pip install --system --no-cache ".[dev]"

COPY src/ src/
COPY alembic.ini alembic.ini
COPY migrations/ migrations/

EXPOSE 8000

CMD ["uvicorn", "tvbf.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

Note: `alembic.ini` and `migrations/` don't exist yet — the `COPY` steps will fail now. They will be added in Task 8 before any `task build` that actually runs this. Leave the Dockerfile as-is.

- [ ] **Step 2: Write .dockerignore**

```
__pycache__
*.pyc
.pytest_cache
.ruff_cache
.git
.gitignore
.venv
node_modules
docs/
```

- [ ] **Step 3: Write docker-compose.yml**

```yaml
services:
  tvbf-backend:
    build: .
    container_name: tvbf_backend
    volumes:
      - ./src:/app/src
      - ./migrations:/app/migrations
      - ./alembic.ini:/app/alembic.ini
      - ./pyproject.toml:/app/pyproject.toml
      - ./tests:/app/tests
    environment:
      DATABASE_URL: "postgresql+asyncpg://root:root@tbc_postgresql_db:5432/tvbf"
      TEST_DATABASE_URL: "postgresql+asyncpg://root:root@tbc_postgresql_db:5432/tvbf_test"
      ADMIN_TOKEN: "${ADMIN_TOKEN:-dev-secret-change-me}"
      TVMAZE_BASE_URL: "https://api.tvmaze.com"
      TVMAZE_RATE_LIMIT_REQUESTS: "18"
      TVMAZE_RATE_LIMIT_WINDOW_SECONDS: "10"
      TVMAZE_RETRY_MAX_ATTEMPTS: "5"
      INGEST_CONSECUTIVE_FAILURE_THRESHOLD: "10"
      INGEST_STALE_RUN_MINUTES: "15"
      LOG_LEVEL: "INFO"
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.tvbf-backend.rule=Host(`tvbf-backend.localhost`)"
      - "traefik.http.routers.tvbf-backend.tls=true"
      - "traefik.http.routers.tvbf-backend.entrypoints=websecure"
      - "traefik.http.services.tvbf-backend.loadbalancer.server.port=8000"
    command: ["uvicorn", "tvbf.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
    networks:
      - proxy

networks:
  proxy:
    external: true
```

- [ ] **Step 4: Task complete**

The build will fail at the `COPY alembic.ini` line until Task 8 creates those files — that's expected. Do not `task build` yet.

---

### Task 3: Taskfile with core container targets

**Files:**
- Create: `tvbf-backend/Taskfile.yml`

- [ ] **Step 1: Write the Taskfile**

```yaml
version: '3'

includes:
  infra:
    taskfile: ../../tbc-localdev-infra/Taskfile.yml
    dir: ../../tbc-localdev-infra

vars:
  COMPOSE: docker compose
  SVC: tvbf-backend
  EXEC: "{{.COMPOSE}} exec {{.SVC}}"

tasks:
  up:
    desc: Start the backend container (requires infra to be up)
    cmds:
      - "{{.COMPOSE}} up -d"

  down:
    desc: Stop the backend container
    cmds:
      - "{{.COMPOSE}} down"

  build:
    desc: Rebuild the backend image
    cmds:
      - "{{.COMPOSE}} build"

  logs:
    desc: Follow backend logs
    cmds:
      - "{{.COMPOSE}} logs -f {{.SVC}}"

  shell:
    desc: Open a bash shell inside the backend container
    cmds:
      - "{{.EXEC}} bash"

  ps:
    desc: Show compose service status
    cmds:
      - "{{.COMPOSE}} ps"
```

- [ ] **Step 2: Verify Taskfile parses**

Run: `task -l`
Expected: lists `up`, `down`, `build`, `logs`, `shell`, `ps` and the included `infra:*` targets.

- [ ] **Step 3: Task complete**

---

### Task 4: Verify the scaffolded container boots

**Files:** none modified.

- [ ] **Step 1: Ensure localdev infra is running**

Run: `task infra:up` (or manually `docker compose -f ../tbc-localdev-infra/docker-compose.yml up -d`).
Verify: `docker ps | grep tbc_postgresql_db` shows the container running.

- [ ] **Step 2: Temporarily neutralize the alembic COPY lines in Dockerfile**

So the image can build before Task 8. Comment out these two lines:

```dockerfile
# COPY alembic.ini alembic.ini
# COPY migrations/ migrations/
```

Uncomment them at the start of Task 8.

- [ ] **Step 3: Build and start**

Run: `task build`
Expected: image builds cleanly.

Run: `task up`
Expected: `tvbf_backend` container starts.

- [ ] **Step 4: Hit /healthz**

Run: `curl -sk https://tvbf-backend.localhost/healthz` (through Traefik), or `curl -s http://localhost:8000/healthz` if exposing the port directly for this check; the Traefik route is the intended path.

Expected: `{"status":"ok"}`

If Traefik returns a TLS error, the localdev infra's cert setup is independent of this plan — use the direct port by temporarily adding `ports: ["8000:8000"]` to the compose service. Remove that line once Traefik works.

- [ ] **Step 5: Run the in-container test**

Run: `task shell` then inside the shell: `pytest tests/test_health.py -v`
Expected: 1 passed.

- [ ] **Step 6: Task complete**

Stop the container with `task down` if you want, but leaving it running is fine — subsequent tasks rebuild it when dependencies change.

---

### Task 5: Configuration module (Pydantic Settings)

**Files:**
- Create: `tvbf-backend/src/tvbf/config.py`
- Create: `tvbf-backend/tests/test_config.py`
- Create: `tvbf-backend/.env.example`

- [ ] **Step 1: Write the failing test**

Create `tests/test_config.py`:

```python
import os

import pytest

from tvbf.config import Settings


def test_settings_reads_database_url_from_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://a:b@c:5432/d")
    monkeypatch.setenv("ADMIN_TOKEN", "xxx")
    s = Settings()
    assert s.database_url == "postgresql+asyncpg://a:b@c:5432/d"
    assert s.admin_token == "xxx"


def test_settings_has_sensible_defaults(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://a:b@c:5432/d")
    monkeypatch.setenv("ADMIN_TOKEN", "xxx")
    s = Settings()
    assert s.tvmaze_base_url == "https://api.tvmaze.com"
    assert s.tvmaze_rate_limit_requests == 18
    assert s.tvmaze_rate_limit_window_seconds == 10
    assert s.tvmaze_retry_max_attempts == 5
    assert s.ingest_consecutive_failure_threshold == 10
    assert s.ingest_stale_run_minutes == 15


def test_settings_requires_admin_token(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://a:b@c:5432/d")
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    with pytest.raises(Exception):
        Settings()
```

- [ ] **Step 2: Run tests, verify failure**

Run: `docker compose exec tvbf-backend pytest tests/test_config.py -v`
Expected: ImportError — `tvbf.config` does not exist yet.

- [ ] **Step 3: Implement config.py**

Create `src/tvbf/config.py`:

```python
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = Field(..., alias="DATABASE_URL")
    test_database_url: str | None = Field(default=None, alias="TEST_DATABASE_URL")
    admin_token: str = Field(..., alias="ADMIN_TOKEN")

    tvmaze_base_url: str = Field(default="https://api.tvmaze.com", alias="TVMAZE_BASE_URL")
    tvmaze_rate_limit_requests: int = Field(default=18, alias="TVMAZE_RATE_LIMIT_REQUESTS")
    tvmaze_rate_limit_window_seconds: int = Field(default=10, alias="TVMAZE_RATE_LIMIT_WINDOW_SECONDS")
    tvmaze_retry_max_attempts: int = Field(default=5, alias="TVMAZE_RETRY_MAX_ATTEMPTS")

    ingest_consecutive_failure_threshold: int = Field(default=10, alias="INGEST_CONSECUTIVE_FAILURE_THRESHOLD")
    ingest_stale_run_minutes: int = Field(default=15, alias="INGEST_STALE_RUN_MINUTES")

    log_level: str = Field(default="INFO", alias="LOG_LEVEL")


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

- [ ] **Step 4: Write .env.example**

```
DATABASE_URL=postgresql+asyncpg://root:root@tbc_postgresql_db:5432/tvbf
TEST_DATABASE_URL=postgresql+asyncpg://root:root@tbc_postgresql_db:5432/tvbf_test
ADMIN_TOKEN=dev-secret-change-me
```

- [ ] **Step 5: Run tests, verify pass**

Run: `docker compose exec tvbf-backend pytest tests/test_config.py -v`
Expected: 3 passed.

- [ ] **Step 6: Task complete**

---

### Task 6: Database engine, session, and base metadata

**Files:**
- Create: `tvbf-backend/src/tvbf/db.py`
- Create: `tvbf-backend/tests/conftest.py`

- [ ] **Step 1: Implement db.py**

Create `src/tvbf/db.py`:

```python
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from tvbf.config import get_settings

_settings = get_settings()

engine = create_async_engine(_settings.database_url, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
```

- [ ] **Step 2: Write the conftest fixtures**

Create `tests/conftest.py`:

```python
import os
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tvbf.db import Base


@pytest.fixture(scope="session")
async def test_engine():
    url = os.environ["TEST_DATABASE_URL"]
    engine = create_async_engine(url, pool_pre_ping=True)
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA IF EXISTS tvmaze CASCADE"))
        await conn.execute(text("DROP SCHEMA IF EXISTS app CASCADE"))
        await conn.execute(text("CREATE SCHEMA tvmaze"))
        await conn.execute(text("CREATE SCHEMA app"))
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA IF EXISTS tvmaze CASCADE"))
        await conn.execute(text("DROP SCHEMA IF EXISTS app CASCADE"))
    await engine.dispose()


@pytest.fixture
async def session(test_engine) -> AsyncIterator[AsyncSession]:
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker() as s:
        yield s
        await s.rollback()
    async with test_engine.begin() as conn:
        result = await conn.execute(
            text(
                "SELECT schemaname || '.' || tablename FROM pg_tables "
                "WHERE schemaname IN ('tvmaze', 'app')"
            )
        )
        tables = [r[0] for r in result]
        if tables:
            await conn.execute(text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE"))
```

Truncation happens inside the `session` fixture's teardown (after the transaction rollback). Tests that don't request `session` don't pull in the DB engine — avoids spurious connection errors on pure-unit tests. Do NOT use `@pytest.fixture(autouse=True)` for this cleanup: autousing on a session-scoped async dep causes pytest-asyncio event-loop mismatches ("Event loop is closed" on teardown). `asyncio_default_fixture_loop_scope = "session"` in pyproject.toml pins the loop scope for session-scoped async fixtures.

- [ ] **Step 3: Task complete**

Tests can't run yet — no models exist. The fixtures will be exercised starting in Task 13.

---

### Task 7: db:init task target (create tvbf + tvbf_test databases)

**Files:**
- Modify: `tvbf-backend/Taskfile.yml`

- [ ] **Step 1: Add db:init to Taskfile**

Append to `Taskfile.yml`:

```yaml
  db:init:
    desc: Create the tvbf and tvbf_test databases in the shared Postgres (idempotent)
    cmds:
      - |
        for db in tvbf tvbf_test; do
          docker exec tbc_postgresql_db psql -U root -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='$db'" | grep -q 1 \
            || docker exec tbc_postgresql_db psql -U root -d postgres -c "CREATE DATABASE $db"
        done
```

- [ ] **Step 2: Run it**

Run: `task db:init`
Expected: no errors. Idempotent — safe to re-run.

Verify: `docker exec tbc_postgresql_db psql -U root -l | grep -E 'tvbf(_test)?'`
Expected: both databases listed.

- [ ] **Step 3: Task complete**

---

### Task 8: Alembic setup

**Files:**
- Create: `tvbf-backend/alembic.ini`
- Create: `tvbf-backend/migrations/env.py`
- Create: `tvbf-backend/migrations/script.py.mako`
- Modify: `tvbf-backend/Dockerfile` (uncomment the COPY lines neutralized in Task 4)

- [ ] **Step 1: Write alembic.ini**

```ini
[alembic]
script_location = migrations
prepend_sys_path = src
timezone = UTC
version_path_separator = os
sqlalchemy.url = driver://user:pass@localhost/db  ; overridden in env.py

[loggers]
keys = root,sqlalchemy,alembic

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = WARN
handlers = console
qualname =

[logger_sqlalchemy]
level = WARN
handlers =
qualname = sqlalchemy.engine

[logger_alembic]
level = INFO
handlers =
qualname = alembic

[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic

[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
datefmt = %H:%M:%S
```

- [ ] **Step 2: Write migrations/env.py**

```python
import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from tvbf.config import get_settings
from tvbf.db import Base

import tvbf.tvmaze.models  # noqa: F401  -- register models with Base.metadata
import tvbf.app.models     # noqa: F401

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", get_settings().database_url)

target_metadata = Base.metadata


def include_name(name, type_, parent_names):
    if type_ == "schema":
        return name in ("tvmaze", "app")
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        include_schemas=True,
        include_name=include_name,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_schemas=True,
        include_name=include_name,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
```

- [ ] **Step 3: Write migrations/script.py.mako**

```mako
"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

"""
from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
```

- [ ] **Step 4: Create empty versions directory**

Create `migrations/versions/.gitkeep` (empty file).

- [ ] **Step 5: Restore the Dockerfile COPY lines neutralized in Task 4**

Uncomment:

```dockerfile
COPY alembic.ini alembic.ini
COPY migrations/ migrations/
```

- [ ] **Step 6: Rebuild the image**

Run: `task build`
Expected: builds cleanly.

- [ ] **Step 7: Add migrate targets to Taskfile**

Append to `Taskfile.yml`:

```yaml
  migrate:
    desc: Run alembic migrations against the tvbf database
    cmds:
      - "{{.EXEC}} alembic upgrade head"

  makemigration:
    desc: 'Create a new autogenerated migration (task makemigration -- "message")'
    cmds:
      - "{{.EXEC}} alembic revision --autogenerate -m \"{{.CLI_ARGS}}\""
```

- [ ] **Step 8: Smoke test alembic**

Run: `task up` then `task shell` then inside: `alembic current`
Expected: no errors, no output (no revisions yet).

- [ ] **Step 9: Task complete**

---

### Task 9: SQLAlchemy models for tvmaze schema (and empty app)

**Files:**
- Create: `tvbf-backend/src/tvbf/tvmaze/__init__.py`
- Create: `tvbf-backend/src/tvbf/tvmaze/models.py`
- Create: `tvbf-backend/src/tvbf/app/__init__.py`
- Create: `tvbf-backend/src/tvbf/app/models.py`

- [ ] **Step 1: Create tvmaze package init**

Create `src/tvbf/tvmaze/__init__.py` (empty).

- [ ] **Step 2: Implement tvmaze/models.py**

Create `src/tvbf/tvmaze/models.py`:

```python
from datetime import date, datetime, time
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    Time,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from tvbf.db import Base

SCHEMA = "tvmaze"


class Network(Base):
    __tablename__ = "network"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    country_code: Mapped[str | None] = mapped_column(Text)
    country_name: Mapped[str | None] = mapped_column(Text)
    timezone: Mapped[str | None] = mapped_column(Text)


class WebChannel(Base):
    __tablename__ = "web_channel"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    country_code: Mapped[str | None] = mapped_column(Text)
    country_name: Mapped[str | None] = mapped_column(Text)
    timezone: Mapped[str | None] = mapped_column(Text)


class Genre(Base):
    __tablename__ = "genre"
    __table_args__ = (
        UniqueConstraint("name", name="uq_genre_name"),
        {"schema": SCHEMA},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)


class Show(Base):
    __tablename__ = "show"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    type: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(Text)
    runtime: Mapped[int | None] = mapped_column(Integer)
    premiered: Mapped[date | None] = mapped_column(Date)
    ended: Mapped[date | None] = mapped_column(Date)
    official_site: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    image_medium: Mapped[str | None] = mapped_column(Text)
    image_original: Mapped[str | None] = mapped_column(Text)
    externals_imdb: Mapped[str | None] = mapped_column(Text)
    externals_tvdb: Mapped[int | None] = mapped_column(Integer)
    externals_tvrage: Mapped[int | None] = mapped_column(Integer)
    network_id: Mapped[int | None] = mapped_column(
        ForeignKey(f"{SCHEMA}.network.id"), nullable=True
    )
    web_channel_id: Mapped[int | None] = mapped_column(
        ForeignKey(f"{SCHEMA}.web_channel.id"), nullable=True
    )
    tvmaze_updated: Mapped[int] = mapped_column(BigInteger, nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Season(Base):
    __tablename__ = "season"
    __table_args__ = (
        UniqueConstraint("show_id", "number", name="uq_season_show_number"),
        {"schema": SCHEMA},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    show_id: Mapped[int] = mapped_column(
        ForeignKey(f"{SCHEMA}.show.id", ondelete="CASCADE"), nullable=False
    )
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str | None] = mapped_column(Text)
    episode_order: Mapped[int | None] = mapped_column(Integer)
    premiere_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    network_id: Mapped[int | None] = mapped_column(ForeignKey(f"{SCHEMA}.network.id"))
    web_channel_id: Mapped[int | None] = mapped_column(ForeignKey(f"{SCHEMA}.web_channel.id"))
    image_medium: Mapped[str | None] = mapped_column(Text)
    image_original: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)


class Episode(Base):
    __tablename__ = "episode"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    show_id: Mapped[int] = mapped_column(
        ForeignKey(f"{SCHEMA}.show.id", ondelete="CASCADE"), nullable=False
    )
    season_id: Mapped[int | None] = mapped_column(
        ForeignKey(f"{SCHEMA}.season.id", ondelete="SET NULL")
    )
    season: Mapped[int] = mapped_column(Integer, nullable=False)
    number: Mapped[int | None] = mapped_column(Integer)
    name: Mapped[str | None] = mapped_column(Text)
    airdate: Mapped[date | None] = mapped_column(Date)
    airtime: Mapped[time | None] = mapped_column(Time)
    runtime: Mapped[int | None] = mapped_column(Integer)
    summary: Mapped[str | None] = mapped_column(Text)
    image_medium: Mapped[str | None] = mapped_column(Text)
    image_original: Mapped[str | None] = mapped_column(Text)


class ShowGenre(Base):
    __tablename__ = "show_genre"
    __table_args__ = (
        PrimaryKeyConstraint("show_id", "genre_id", name="pk_show_genre"),
        {"schema": SCHEMA},
    )

    show_id: Mapped[int] = mapped_column(
        ForeignKey(f"{SCHEMA}.show.id", ondelete="CASCADE"), nullable=False
    )
    genre_id: Mapped[int] = mapped_column(
        ForeignKey(f"{SCHEMA}.genre.id"), nullable=False
    )


class IngestRun(Base):
    __tablename__ = "ingest_run"
    __table_args__ = (
        CheckConstraint("kind IN ('initial', 'update')", name="ck_ingest_run_kind"),
        CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'cancelled')",
            name="ck_ingest_run_status",
        ),
        {"schema": SCHEMA},
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_update_cursor: Mapped[int | None] = mapped_column(BigInteger)
    shows_processed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    shows_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_progress_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
```

- [ ] **Step 3: Create app package**

Create `src/tvbf/app/__init__.py` (empty).

Create `src/tvbf/app/models.py`:

```python
# Placeholder. The `app` schema is created empty in this milestone; user-facing
# models (users, friend connections, watch tracking) will land in a later plan.
```

- [ ] **Step 4: Task complete**

---

### Task 10: Generate and apply the initial migration

**Files:**
- Create: `tvbf-backend/migrations/versions/0001_*.py` (alembic will name it)

- [ ] **Step 1: Ensure tvbf database is migratable**

Run: `task db:init` (idempotent).

- [ ] **Step 2: Create schemas manually in tvbf**

Alembic's autogenerate needs schemas to exist in the target DB to detect tables correctly. Run:

```bash
docker exec tbc_postgresql_db psql -U root -d tvbf -v ON_ERROR_STOP=1 -c "CREATE SCHEMA IF NOT EXISTS tvmaze; CREATE SCHEMA IF NOT EXISTS app;"
```

- [ ] **Step 3: Autogenerate the migration**

Run: `task makemigration -- "initial tvmaze schema"`
Expected: a file like `migrations/versions/<hash>_initial_tvmaze_schema.py` is created.

- [ ] **Step 4: Review the generated migration**

Open the generated file. Confirm it:
- Creates all 8 tables (`network`, `web_channel`, `genre`, `show`, `season`, `episode`, `show_genre`, `ingest_run`) under `schema='tvmaze'`.
- Adds the unique constraints and check constraints named in `models.py`.
- Does NOT try to drop/recreate schemas (if it does, remove those operations — the `task db:init` step owns schema creation).

Remove any `sa.Column(..., default=<python callable>, ...)` that alembic emitted as a Python-side default where you intended a server default; replace with `server_default=...` if needed.

- [ ] **Step 5: Apply the migration**

Run: `task migrate`
Expected: `Running upgrade -> <hash>, initial tvmaze schema` in output. No errors.

Verify:
```bash
docker exec tbc_postgresql_db psql -U root -d tvbf -c "\dt tvmaze.*"
```
Expected: all 8 tables listed.

- [ ] **Step 6: Task complete**

---

### Task 11: Pydantic schemas for TV Maze API payloads

**Files:**
- Create: `tvbf-backend/src/tvbf/tvmaze/schemas.py`

- [ ] **Step 1: Implement schemas.py**

Create `src/tvbf/tvmaze/schemas.py`:

```python
from datetime import date, time
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field


def _empty_to_none(v: Any) -> Any:
    if v == "":
        return None
    return v


# TV Maze returns empty strings rather than null for unknown date/time values.
OptionalDate = Annotated[date | None, BeforeValidator(_empty_to_none)]
OptionalTime = Annotated[time | None, BeforeValidator(_empty_to_none)]


class TVMazeNetwork(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    name: str
    country: dict | None = None

    @property
    def country_code(self) -> str | None:
        return (self.country or {}).get("code")

    @property
    def country_name(self) -> str | None:
        return (self.country or {}).get("name")

    @property
    def timezone(self) -> str | None:
        return (self.country or {}).get("timezone")


class TVMazeImage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    medium: str | None = None
    original: str | None = None


class TVMazeEpisode(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    season: int
    number: int | None = None
    name: str | None = None
    airdate: OptionalDate = None
    airtime: OptionalTime = None
    runtime: int | None = None
    summary: str | None = None
    image: TVMazeImage | None = None


class TVMazeSeason(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    number: int
    name: str | None = None
    episodeOrder: int | None = None
    premiereDate: OptionalDate = None
    endDate: OptionalDate = None
    network: TVMazeNetwork | None = None
    webChannel: TVMazeNetwork | None = None
    image: TVMazeImage | None = None
    summary: str | None = None


class TVMazeExternals(BaseModel):
    model_config = ConfigDict(extra="ignore")

    imdb: str | None = None
    tvdb: int | None = None
    tvrage: int | None = None


class TVMazeEmbedded(BaseModel):
    model_config = ConfigDict(extra="ignore")

    episodes: list[TVMazeEpisode] = Field(default_factory=list)
    seasons: list[TVMazeSeason] = Field(default_factory=list)


class TVMazeShow(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: int
    name: str
    type: str | None = None
    language: str | None = None
    status: str | None = None
    runtime: int | None = None
    premiered: OptionalDate = None
    ended: OptionalDate = None
    officialSite: str | None = None
    summary: str | None = None
    image: TVMazeImage | None = None
    externals: TVMazeExternals | None = None
    network: TVMazeNetwork | None = None
    webChannel: TVMazeNetwork | None = None
    genres: list[str] = Field(default_factory=list)
    updated: int
    embedded: TVMazeEmbedded = Field(default_factory=TVMazeEmbedded, alias="_embedded")
```

- [ ] **Step 2: Task complete**

No unit tests for the schemas themselves — they're exercised indirectly via the client and upsert tests.

---

### Task 12: Rate-limited TV Maze HTTP client

**Files:**
- Create: `tvbf-backend/src/tvbf/tvmaze/client.py`
- Create: `tvbf-backend/tests/test_tvmaze_client.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_tvmaze_client.py`:

```python
import time

import httpx
import pytest
import respx

from tvbf.tvmaze.client import RateLimiter, TVMazeClient


async def test_rate_limiter_enforces_rate():
    limiter = RateLimiter(calls=3, window_seconds=1)
    start = time.monotonic()
    for _ in range(6):
        await limiter.acquire()
    elapsed = time.monotonic() - start
    assert elapsed >= 1.0, f"6 calls at 3/s should take >= 1s, took {elapsed:.3f}s"


@respx.mock
async def test_client_fetches_show_with_embeds():
    respx.get("https://api.tvmaze.com/shows/1").mock(
        return_value=httpx.Response(200, json={"id": 1, "name": "Under the Dome", "updated": 1, "genres": []})
    )
    async with TVMazeClient(base_url="https://api.tvmaze.com", rate_calls=20, rate_window=1) as c:
        payload = await c.get_show(1)
    assert payload["id"] == 1
    assert respx.calls.last.request.url.params.get_list("embed[]") == ["episodes", "seasons"]


@respx.mock
async def test_client_fetches_updates_shows():
    respx.get("https://api.tvmaze.com/updates/shows").mock(
        return_value=httpx.Response(200, json={"1": 100, "2": 200})
    )
    async with TVMazeClient(base_url="https://api.tvmaze.com", rate_calls=20, rate_window=1) as c:
        updates = await c.get_show_updates()
    assert updates == {1: 100, 2: 200}


@respx.mock
async def test_client_retries_on_5xx_then_succeeds():
    route = respx.get("https://api.tvmaze.com/shows/42").mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(503),
            httpx.Response(200, json={"id": 42, "name": "ok", "updated": 1, "genres": []}),
        ]
    )
    async with TVMazeClient(
        base_url="https://api.tvmaze.com", rate_calls=20, rate_window=1, retry_max_attempts=3,
        retry_base_delay=0.01,
    ) as c:
        payload = await c.get_show(42)
    assert payload["id"] == 42
    assert route.call_count == 3


@respx.mock
async def test_client_honors_retry_after_on_429():
    route = respx.get("https://api.tvmaze.com/shows/7").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(200, json={"id": 7, "name": "ok", "updated": 1, "genres": []}),
        ]
    )
    async with TVMazeClient(
        base_url="https://api.tvmaze.com", rate_calls=20, rate_window=1, retry_max_attempts=3,
        retry_base_delay=0.01,
    ) as c:
        payload = await c.get_show(7)
    assert payload["id"] == 7
    assert route.call_count == 2


@respx.mock
async def test_client_does_not_retry_on_404():
    respx.get("https://api.tvmaze.com/shows/9999").mock(return_value=httpx.Response(404))
    async with TVMazeClient(
        base_url="https://api.tvmaze.com", rate_calls=20, rate_window=1, retry_max_attempts=3,
        retry_base_delay=0.01,
    ) as c:
        with pytest.raises(httpx.HTTPStatusError):
            await c.get_show(9999)
```

- [ ] **Step 2: Run tests, verify failure**

Run: `docker compose exec tvbf-backend pytest tests/test_tvmaze_client.py -v`
Expected: ImportError — `tvbf.tvmaze.client` does not exist.

- [ ] **Step 3: Implement client.py**

Create `src/tvbf/tvmaze/client.py`:

```python
import asyncio
import time
from collections import deque

import httpx


class RateLimiter:
    """Sliding-window token bucket. Allows up to `calls` calls per `window_seconds`."""

    def __init__(self, calls: int, window_seconds: float):
        self._calls = calls
        self._window = window_seconds
        self._timestamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            while self._timestamps and now - self._timestamps[0] >= self._window:
                self._timestamps.popleft()
            if len(self._timestamps) >= self._calls:
                wait = self._window - (now - self._timestamps[0])
                if wait > 0:
                    await asyncio.sleep(wait)
                    now = time.monotonic()
                    while self._timestamps and now - self._timestamps[0] >= self._window:
                        self._timestamps.popleft()
            self._timestamps.append(time.monotonic())


class TVMazeClient:
    def __init__(
        self,
        base_url: str,
        rate_calls: int,
        rate_window: float,
        retry_max_attempts: int = 5,
        retry_base_delay: float = 0.5,
        timeout: float = 30.0,
    ):
        self._base_url = base_url.rstrip("/")
        self._limiter = RateLimiter(rate_calls, rate_window)
        self._retry_max = retry_max_attempts
        self._retry_base = retry_base_delay
        self._client = httpx.AsyncClient(timeout=timeout)

    async def __aenter__(self) -> "TVMazeClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self._client.aclose()

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        attempt = 0
        while True:
            await self._limiter.acquire()
            try:
                resp = await self._client.request(method, url, **kwargs)
            except (httpx.TimeoutException, httpx.NetworkError):
                if attempt + 1 >= self._retry_max:
                    raise
                await asyncio.sleep(self._retry_base * (2**attempt))
                attempt += 1
                continue

            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                wait = float(retry_after) if retry_after is not None else self._retry_base * (2**attempt)
                await asyncio.sleep(wait)
                continue  # 429 does not count against retry budget

            if 500 <= resp.status_code < 600:
                if attempt + 1 >= self._retry_max:
                    resp.raise_for_status()
                await asyncio.sleep(self._retry_base * (2**attempt))
                attempt += 1
                continue

            resp.raise_for_status()
            return resp

    async def get_show(self, show_id: int) -> dict:
        url = f"{self._base_url}/shows/{show_id}"
        resp = await self._request("GET", url, params=[("embed[]", "episodes"), ("embed[]", "seasons")])
        return resp.json()

    async def get_show_updates(self) -> dict[int, int]:
        url = f"{self._base_url}/updates/shows"
        resp = await self._request("GET", url)
        return {int(k): int(v) for k, v in resp.json().items()}
```

- [ ] **Step 4: Run tests, verify pass**

Run: `docker compose exec tvbf-backend pytest tests/test_tvmaze_client.py -v`
Expected: 6 passed.

- [ ] **Step 5: Task complete**

---

### Task 13: Upsert functions for networks, web channels, and genres

**Files:**
- Create: `tvbf-backend/src/tvbf/tvmaze/upsert.py`
- Create: `tvbf-backend/tests/test_upsert.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_upsert.py`:

```python
from sqlalchemy import select

from tvbf.tvmaze import models as m
from tvbf.tvmaze.schemas import TVMazeNetwork
from tvbf.tvmaze.upsert import upsert_genre_by_name, upsert_network, upsert_web_channel


async def test_upsert_network_inserts_and_updates(session):
    net = TVMazeNetwork.model_validate(
        {"id": 1, "name": "CBS", "country": {"code": "US", "name": "USA", "timezone": "America/New_York"}}
    )
    net_id = await upsert_network(session, net)
    await session.commit()

    row = (await session.execute(select(m.Network).where(m.Network.id == net_id))).scalar_one()
    assert row.name == "CBS"
    assert row.country_code == "US"

    net2 = TVMazeNetwork.model_validate(
        {"id": 1, "name": "CBS (renamed)", "country": {"code": "US"}}
    )
    await upsert_network(session, net2)
    await session.commit()
    row = (await session.execute(select(m.Network).where(m.Network.id == 1))).scalar_one()
    assert row.name == "CBS (renamed)"


async def test_upsert_web_channel_inserts(session):
    wc = TVMazeNetwork.model_validate({"id": 91, "name": "Netflix", "country": None})
    wc_id = await upsert_web_channel(session, wc)
    await session.commit()
    row = (await session.execute(select(m.WebChannel).where(m.WebChannel.id == wc_id))).scalar_one()
    assert row.name == "Netflix"
    assert row.country_code is None


async def test_upsert_network_accepts_none_returns_none(session):
    assert await upsert_network(session, None) is None
    assert await upsert_web_channel(session, None) is None


async def test_upsert_genre_by_name_is_idempotent(session):
    a = await upsert_genre_by_name(session, "Drama")
    b = await upsert_genre_by_name(session, "Drama")
    await session.commit()
    assert a == b

    c = await upsert_genre_by_name(session, "Comedy")
    assert c != a

    rows = (await session.execute(select(m.Genre))).scalars().all()
    assert {r.name for r in rows} == {"Drama", "Comedy"}
```

- [ ] **Step 2: Run tests, verify failure**

Run: `docker compose exec tvbf-backend pytest tests/test_upsert.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement the network/web_channel/genre upserts**

Create `src/tvbf/tvmaze/upsert.py`:

```python
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.tvmaze import models as m
from tvbf.tvmaze.schemas import TVMazeNetwork


async def upsert_network(session: AsyncSession, net: TVMazeNetwork | None) -> int | None:
    if net is None:
        return None
    stmt = (
        insert(m.Network)
        .values(
            id=net.id,
            name=net.name,
            country_code=net.country_code,
            country_name=net.country_name,
            timezone=net.timezone,
        )
        .on_conflict_do_update(
            index_elements=[m.Network.id],
            set_={
                "name": net.name,
                "country_code": net.country_code,
                "country_name": net.country_name,
                "timezone": net.timezone,
            },
        )
    )
    await session.execute(stmt)
    return net.id


async def upsert_web_channel(session: AsyncSession, wc: TVMazeNetwork | None) -> int | None:
    if wc is None:
        return None
    stmt = (
        insert(m.WebChannel)
        .values(
            id=wc.id,
            name=wc.name,
            country_code=wc.country_code,
            country_name=wc.country_name,
            timezone=wc.timezone,
        )
        .on_conflict_do_update(
            index_elements=[m.WebChannel.id],
            set_={
                "name": wc.name,
                "country_code": wc.country_code,
                "country_name": wc.country_name,
                "timezone": wc.timezone,
            },
        )
    )
    await session.execute(stmt)
    return wc.id


async def upsert_genre_by_name(session: AsyncSession, name: str) -> int:
    existing = (
        await session.execute(select(m.Genre.id).where(m.Genre.name == name))
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    stmt = (
        insert(m.Genre)
        .values(name=name)
        .on_conflict_do_nothing(index_elements=[m.Genre.name])
        .returning(m.Genre.id)
    )
    result = (await session.execute(stmt)).scalar_one_or_none()
    if result is not None:
        return result
    return (await session.execute(select(m.Genre.id).where(m.Genre.name == name))).scalar_one()
```

- [ ] **Step 4: Run tests, verify pass**

Run: `docker compose exec tvbf-backend pytest tests/test_upsert.py -v`
Expected: 4 passed.

- [ ] **Step 5: Task complete**

---

### Task 14: Upsert seasons

**Files:**
- Modify: `tvbf-backend/src/tvbf/tvmaze/upsert.py`
- Modify: `tvbf-backend/tests/test_upsert.py`

- [ ] **Step 1: Add the failing tests**

Append to `tests/test_upsert.py`:

```python
from tvbf.tvmaze.schemas import TVMazeSeason
from tvbf.tvmaze.upsert import upsert_season


async def test_upsert_season_inserts_with_fks(session):
    # Prerequisite: show exists (seasons FK to show).
    session.add(m.Show(id=100, name="S", tvmaze_updated=1))
    await session.commit()

    net = TVMazeNetwork.model_validate({"id": 5, "name": "BBC", "country": {"code": "GB"}})
    await upsert_network(session, net)
    season = TVMazeSeason.model_validate(
        {
            "id": 555, "number": 1, "name": "Season 1", "episodeOrder": 10,
            "premiereDate": "2020-01-01", "endDate": "2020-03-01",
            "network": {"id": 5, "name": "BBC", "country": {"code": "GB"}},
            "webChannel": None, "image": {"medium": "m.jpg", "original": "o.jpg"},
            "summary": "<p>summary</p>",
        }
    )
    await session.commit()

    sid = await upsert_season(session, show_id=100, season=season)
    await session.commit()
    assert sid == 555

    row = (await session.execute(select(m.Season).where(m.Season.id == 555))).scalar_one()
    assert row.show_id == 100
    assert row.number == 1
    assert row.name == "Season 1"
    assert row.episode_order == 10
    assert row.network_id == 5
    assert row.web_channel_id is None
    assert row.image_medium == "m.jpg"


async def test_upsert_season_is_idempotent(session):
    session.add(m.Show(id=101, name="S", tvmaze_updated=1))
    await session.commit()
    season = TVMazeSeason.model_validate({"id": 556, "number": 1})
    await upsert_season(session, 101, season)
    await upsert_season(session, 101, season)
    await session.commit()
    count = (await session.execute(select(m.Season).where(m.Season.id == 556))).scalars().all()
    assert len(count) == 1
```

- [ ] **Step 2: Run tests, verify failure**

Run: `docker compose exec tvbf-backend pytest tests/test_upsert.py -v`
Expected: the two new tests fail; previous tests still pass.

- [ ] **Step 3: Implement upsert_season**

Append to `src/tvbf/tvmaze/upsert.py`:

```python
from tvbf.tvmaze.schemas import TVMazeSeason


async def upsert_season(session: AsyncSession, show_id: int, season: TVMazeSeason) -> int:
    network_id = await upsert_network(session, season.network)
    web_channel_id = await upsert_web_channel(session, season.webChannel)
    values = {
        "id": season.id,
        "show_id": show_id,
        "number": season.number,
        "name": season.name,
        "episode_order": season.episodeOrder,
        "premiere_date": season.premiereDate,
        "end_date": season.endDate,
        "network_id": network_id,
        "web_channel_id": web_channel_id,
        "image_medium": season.image.medium if season.image else None,
        "image_original": season.image.original if season.image else None,
        "summary": season.summary,
    }
    stmt = (
        insert(m.Season)
        .values(**values)
        .on_conflict_do_update(
            index_elements=[m.Season.id],
            set_={k: v for k, v in values.items() if k not in ("id", "show_id")},
        )
    )
    await session.execute(stmt)
    return season.id
```

- [ ] **Step 4: Run tests, verify pass**

Run: `docker compose exec tvbf-backend pytest tests/test_upsert.py -v`
Expected: all passed.

- [ ] **Step 5: Task complete**

---

### Task 15: Upsert show (+ genres + show_genre links)

**Files:**
- Modify: `tvbf-backend/src/tvbf/tvmaze/upsert.py`
- Modify: `tvbf-backend/tests/test_upsert.py`

- [ ] **Step 1: Add the failing tests**

Append to `tests/test_upsert.py`:

```python
from tvbf.tvmaze.schemas import TVMazeShow
from tvbf.tvmaze.upsert import upsert_show


async def test_upsert_show_inserts_with_genres_and_network(session):
    payload = TVMazeShow.model_validate(
        {
            "id": 200, "name": "Sherlock", "type": "Scripted", "language": "English",
            "status": "Ended", "runtime": 90, "premiered": "2010-07-25", "ended": "2017-01-15",
            "officialSite": "https://example.com",
            "summary": "<p>ok</p>",
            "image": {"medium": "m", "original": "o"},
            "externals": {"imdb": "tt1475582", "tvdb": 176941, "tvrage": 19718},
            "network": {"id": 12, "name": "BBC One", "country": {"code": "GB"}},
            "webChannel": None,
            "genres": ["Drama", "Crime", "Mystery"],
            "updated": 1700000000,
            "_embedded": {"episodes": [], "seasons": []},
        }
    )
    await upsert_show(session, payload)
    await session.commit()

    row = (await session.execute(select(m.Show).where(m.Show.id == 200))).scalar_one()
    assert row.name == "Sherlock"
    assert row.network_id == 12
    assert row.web_channel_id is None
    assert row.externals_imdb == "tt1475582"
    assert row.tvmaze_updated == 1700000000

    links = (
        await session.execute(
            select(m.Genre.name).join(m.ShowGenre, m.ShowGenre.genre_id == m.Genre.id).where(m.ShowGenre.show_id == 200)
        )
    ).scalars().all()
    assert set(links) == {"Drama", "Crime", "Mystery"}


async def test_upsert_show_replaces_genre_links_on_update(session):
    base = {
        "id": 201, "name": "X", "updated": 1, "network": None, "webChannel": None,
        "genres": ["Drama", "Crime"], "_embedded": {"episodes": [], "seasons": []},
    }
    await upsert_show(session, TVMazeShow.model_validate(base))
    await session.commit()

    base2 = dict(base, genres=["Comedy"])
    await upsert_show(session, TVMazeShow.model_validate(base2))
    await session.commit()

    links = (
        await session.execute(
            select(m.Genre.name).join(m.ShowGenre, m.ShowGenre.genre_id == m.Genre.id).where(m.ShowGenre.show_id == 201)
        )
    ).scalars().all()
    assert set(links) == {"Comedy"}
```

- [ ] **Step 2: Run tests, verify failure**

Run: `docker compose exec tvbf-backend pytest tests/test_upsert.py -v`
Expected: new tests fail with ImportError or NameError.

- [ ] **Step 3: Implement upsert_show**

Append to `src/tvbf/tvmaze/upsert.py`:

```python
from sqlalchemy import delete

from tvbf.tvmaze.schemas import TVMazeShow


async def upsert_show(session: AsyncSession, show: TVMazeShow) -> int:
    network_id = await upsert_network(session, show.network)
    web_channel_id = await upsert_web_channel(session, show.webChannel)

    values = {
        "id": show.id,
        "name": show.name,
        "type": show.type,
        "language": show.language,
        "status": show.status,
        "runtime": show.runtime,
        "premiered": show.premiered,
        "ended": show.ended,
        "official_site": show.officialSite,
        "summary": show.summary,
        "image_medium": show.image.medium if show.image else None,
        "image_original": show.image.original if show.image else None,
        "externals_imdb": show.externals.imdb if show.externals else None,
        "externals_tvdb": show.externals.tvdb if show.externals else None,
        "externals_tvrage": show.externals.tvrage if show.externals else None,
        "network_id": network_id,
        "web_channel_id": web_channel_id,
        "tvmaze_updated": show.updated,
    }
    stmt = (
        insert(m.Show)
        .values(**values)
        .on_conflict_do_update(
            index_elements=[m.Show.id],
            set_={k: v for k, v in values.items() if k != "id"},
        )
    )
    await session.execute(stmt)

    # Replace show<->genre links.
    await session.execute(delete(m.ShowGenre).where(m.ShowGenre.show_id == show.id))
    for name in show.genres:
        gid = await upsert_genre_by_name(session, name)
        await session.execute(
            insert(m.ShowGenre).values(show_id=show.id, genre_id=gid).on_conflict_do_nothing()
        )
    return show.id
```

- [ ] **Step 4: Run tests, verify pass**

Run: `docker compose exec tvbf-backend pytest tests/test_upsert.py -v`
Expected: all passed.

- [ ] **Step 5: Task complete**

---

### Task 16: Upsert episodes with season_id resolution

**Files:**
- Modify: `tvbf-backend/src/tvbf/tvmaze/upsert.py`
- Modify: `tvbf-backend/tests/test_upsert.py`

- [ ] **Step 1: Add the failing tests**

Append to `tests/test_upsert.py`:

```python
from tvbf.tvmaze.schemas import TVMazeEpisode
from tvbf.tvmaze.upsert import upsert_episodes


async def test_upsert_episodes_resolves_season_id(session):
    # Shape: show with two seasons; episodes reference by season number.
    session.add(m.Show(id=300, name="S", tvmaze_updated=1))
    session.add(m.Season(id=3000, show_id=300, number=1))
    session.add(m.Season(id=3001, show_id=300, number=2))
    await session.commit()

    eps = [
        TVMazeEpisode.model_validate({"id": 1, "season": 1, "number": 1, "name": "Pilot", "airdate": "2020-01-01"}),
        TVMazeEpisode.model_validate({"id": 2, "season": 1, "number": 2, "name": "Two"}),
        TVMazeEpisode.model_validate({"id": 3, "season": 2, "number": 1, "name": "S2E1"}),
        TVMazeEpisode.model_validate({"id": 4, "season": 99, "number": 1, "name": "Orphan"}),
    ]
    await upsert_episodes(session, show_id=300, episodes=eps)
    await session.commit()

    rows = (
        await session.execute(select(m.Episode).where(m.Episode.show_id == 300).order_by(m.Episode.id))
    ).scalars().all()
    by_id = {r.id: r for r in rows}
    assert by_id[1].season_id == 3000
    assert by_id[2].season_id == 3000
    assert by_id[3].season_id == 3001
    assert by_id[4].season_id is None  # unknown season number


async def test_upsert_episodes_is_idempotent_and_updates(session):
    session.add(m.Show(id=301, name="S", tvmaze_updated=1))
    session.add(m.Season(id=4000, show_id=301, number=1))
    await session.commit()

    ep_v1 = TVMazeEpisode.model_validate({"id": 10, "season": 1, "number": 1, "name": "v1"})
    ep_v2 = TVMazeEpisode.model_validate({"id": 10, "season": 1, "number": 1, "name": "v2"})
    await upsert_episodes(session, 301, [ep_v1])
    await session.commit()
    await upsert_episodes(session, 301, [ep_v2])
    await session.commit()

    row = (await session.execute(select(m.Episode).where(m.Episode.id == 10))).scalar_one()
    assert row.name == "v2"
    assert row.season_id == 4000
```

- [ ] **Step 2: Run tests, verify failure**

Run: `docker compose exec tvbf-backend pytest tests/test_upsert.py -v`
Expected: the two new tests fail.

- [ ] **Step 3: Implement upsert_episodes**

Append to `src/tvbf/tvmaze/upsert.py`:

```python
from tvbf.tvmaze.schemas import TVMazeEpisode


async def upsert_episodes(
    session: AsyncSession, show_id: int, episodes: list[TVMazeEpisode]
) -> None:
    if not episodes:
        return

    # Build a lookup of (show_id, season_number) -> season.id from the DB.
    season_rows = (
        await session.execute(
            select(m.Season.id, m.Season.number).where(m.Season.show_id == show_id)
        )
    ).all()
    season_by_number = {r.number: r.id for r in season_rows}

    values_list = [
        {
            "id": ep.id,
            "show_id": show_id,
            "season_id": season_by_number.get(ep.season),
            "season": ep.season,
            "number": ep.number,
            "name": ep.name,
            "airdate": ep.airdate,
            "airtime": ep.airtime,
            "runtime": ep.runtime,
            "summary": ep.summary,
            "image_medium": ep.image.medium if ep.image else None,
            "image_original": ep.image.original if ep.image else None,
        }
        for ep in episodes
    ]
    stmt = insert(m.Episode).values(values_list)
    update_cols = {c: getattr(stmt.excluded, c) for c in values_list[0] if c != "id"}
    stmt = stmt.on_conflict_do_update(index_elements=[m.Episode.id], set_=update_cols)
    await session.execute(stmt)
```

- [ ] **Step 4: Run tests, verify pass**

Run: `docker compose exec tvbf-backend pytest tests/test_upsert.py -v`
Expected: all passed.

- [ ] **Step 5: Task complete**

---

### Task 17: Per-show orchestration (upsert_show_payload)

**Files:**
- Modify: `tvbf-backend/src/tvbf/tvmaze/upsert.py`
- Modify: `tvbf-backend/tests/test_upsert.py`

- [ ] **Step 1: Add the failing test**

Append to `tests/test_upsert.py`:

```python
from tvbf.tvmaze.upsert import upsert_show_payload


async def test_upsert_show_payload_inserts_everything(session):
    payload = TVMazeShow.model_validate(
        {
            "id": 400, "name": "Atlanta", "type": "Scripted", "status": "Ended",
            "genres": ["Drama", "Comedy"], "updated": 1700000000,
            "network": {"id": 21, "name": "FX", "country": {"code": "US"}},
            "webChannel": None,
            "_embedded": {
                "seasons": [
                    {"id": 10000, "number": 1, "name": "S1", "episodeOrder": 2},
                    {"id": 10001, "number": 2, "name": "S2", "episodeOrder": 2},
                ],
                "episodes": [
                    {"id": 20000, "season": 1, "number": 1, "name": "E1"},
                    {"id": 20001, "season": 1, "number": 2, "name": "E2"},
                    {"id": 20002, "season": 2, "number": 1, "name": "E3"},
                    {"id": 20003, "season": 2, "number": 2, "name": "E4"},
                ],
            },
        }
    )
    await upsert_show_payload(session, payload)
    await session.commit()

    show = (await session.execute(select(m.Show).where(m.Show.id == 400))).scalar_one()
    assert show.network_id == 21

    seasons = (await session.execute(select(m.Season).where(m.Season.show_id == 400))).scalars().all()
    assert {s.number for s in seasons} == {1, 2}

    eps = (await session.execute(select(m.Episode).where(m.Episode.show_id == 400))).scalars().all()
    assert len(eps) == 4
    assert all(e.season_id is not None for e in eps)
```

- [ ] **Step 2: Run test, verify failure**

Run: `docker compose exec tvbf-backend pytest tests/test_upsert.py::test_upsert_show_payload_inserts_everything -v`
Expected: fail with ImportError / NameError.

- [ ] **Step 3: Implement upsert_show_payload**

Append to `src/tvbf/tvmaze/upsert.py`:

```python
async def upsert_show_payload(session: AsyncSession, show: TVMazeShow) -> int:
    """Upsert a complete show payload (show + its genres + seasons + episodes) in order.

    Caller owns transaction boundaries (commit/rollback).
    """
    await upsert_show(session, show)
    for season in show.embedded.seasons:
        await upsert_season(session, show_id=show.id, season=season)
    await upsert_episodes(session, show_id=show.id, episodes=show.embedded.episodes)
    return show.id
```

- [ ] **Step 4: Run tests, verify pass**

Run: `docker compose exec tvbf-backend pytest tests/test_upsert.py -v`
Expected: all tests pass.

- [ ] **Step 5: Task complete**

---

### Task 18: ingest_run CRUD helpers

**Files:**
- Create: `tvbf-backend/src/tvbf/tvmaze/runs.py`
- Create: `tvbf-backend/tests/test_runs.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_runs.py`:

```python
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import select

from tvbf.tvmaze import models as m
from tvbf.tvmaze.runs import (
    create_run,
    finalize_run,
    get_last_successful_cursor,
    mark_stale_runs_cancelled,
    record_progress,
)


async def test_create_run_inserts_with_running_status(session):
    run_id = await create_run(session, kind="initial")
    await session.commit()
    assert isinstance(run_id, UUID)
    row = (await session.execute(select(m.IngestRun).where(m.IngestRun.id == run_id))).scalar_one()
    assert row.kind == "initial"
    assert row.status == "running"
    assert row.shows_processed == 0


async def test_record_progress_increments_counters_and_stamps(session):
    run_id = await create_run(session, kind="update")
    await session.commit()
    await record_progress(session, run_id, processed_delta=2, failed_delta=1)
    await session.commit()
    row = (await session.execute(select(m.IngestRun).where(m.IngestRun.id == run_id))).scalar_one()
    assert row.shows_processed == 2
    assert row.shows_failed == 1
    assert row.last_progress_at is not None


async def test_finalize_run_sets_status_and_cursor(session):
    run_id = await create_run(session, kind="initial")
    await session.commit()
    await finalize_run(session, run_id, status="succeeded", last_update_cursor=42)
    await session.commit()
    row = (await session.execute(select(m.IngestRun).where(m.IngestRun.id == run_id))).scalar_one()
    assert row.status == "succeeded"
    assert row.last_update_cursor == 42
    assert row.finished_at is not None


async def test_get_last_successful_cursor_returns_latest(session):
    r1 = await create_run(session, kind="initial")
    r2 = await create_run(session, kind="update")
    await session.commit()
    await finalize_run(session, r1, status="succeeded", last_update_cursor=10)
    await finalize_run(session, r2, status="succeeded", last_update_cursor=20)
    await session.commit()
    assert await get_last_successful_cursor(session) == 20


async def test_get_last_successful_cursor_none_when_no_runs(session):
    assert await get_last_successful_cursor(session) is None


async def test_mark_stale_runs_cancelled(session):
    fresh = await create_run(session, kind="initial")
    stale = await create_run(session, kind="initial")
    await session.commit()

    # Force stale's last_progress_at to be old.
    stale_row = (
        await session.execute(select(m.IngestRun).where(m.IngestRun.id == stale))
    ).scalar_one()
    stale_row.last_progress_at = datetime.now(timezone.utc) - timedelta(hours=1)
    fresh_row = (
        await session.execute(select(m.IngestRun).where(m.IngestRun.id == fresh))
    ).scalar_one()
    fresh_row.last_progress_at = datetime.now(timezone.utc)
    await session.commit()

    cancelled = await mark_stale_runs_cancelled(session, stale_after_minutes=15)
    await session.commit()
    assert cancelled == 1

    stale_row = (
        await session.execute(select(m.IngestRun).where(m.IngestRun.id == stale))
    ).scalar_one()
    fresh_row = (
        await session.execute(select(m.IngestRun).where(m.IngestRun.id == fresh))
    ).scalar_one()
    assert stale_row.status == "cancelled"
    assert fresh_row.status == "running"
```

- [ ] **Step 2: Run tests, verify failure**

Run: `docker compose exec tvbf-backend pytest tests/test_runs.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement runs.py**

Create `src/tvbf/tvmaze/runs.py`:

```python
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy import desc, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.tvmaze import models as m


async def create_run(session: AsyncSession, kind: str) -> UUID:
    run = m.IngestRun(id=uuid4(), kind=kind, status="running")
    session.add(run)
    await session.flush()
    return run.id


async def record_progress(
    session: AsyncSession, run_id: UUID, processed_delta: int = 0, failed_delta: int = 0
) -> None:
    now = datetime.now(timezone.utc)
    await session.execute(
        update(m.IngestRun)
        .where(m.IngestRun.id == run_id)
        .values(
            shows_processed=m.IngestRun.shows_processed + processed_delta,
            shows_failed=m.IngestRun.shows_failed + failed_delta,
            last_progress_at=now,
        )
    )


async def finalize_run(
    session: AsyncSession,
    run_id: UUID,
    *,
    status: str,
    last_update_cursor: int | None = None,
    error: str | None = None,
) -> None:
    now = datetime.now(timezone.utc)
    values = {"status": status, "finished_at": now}
    if last_update_cursor is not None:
        values["last_update_cursor"] = last_update_cursor
    if error is not None:
        values["error"] = error
    await session.execute(update(m.IngestRun).where(m.IngestRun.id == run_id).values(**values))


async def get_last_successful_cursor(session: AsyncSession) -> int | None:
    result = await session.execute(
        select(m.IngestRun.last_update_cursor)
        .where(m.IngestRun.status == "succeeded", m.IngestRun.last_update_cursor.is_not(None))
        .order_by(desc(m.IngestRun.finished_at))
        .limit(1)
    )
    return result.scalar_one_or_none()


async def mark_stale_runs_cancelled(session: AsyncSession, *, stale_after_minutes: int) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=stale_after_minutes)
    result = await session.execute(
        update(m.IngestRun)
        .where(
            m.IngestRun.status == "running",
            m.IngestRun.last_progress_at.is_not(None),
            m.IngestRun.last_progress_at < cutoff,
        )
        .values(
            status="cancelled",
            finished_at=datetime.now(timezone.utc),
            error="cancelled by startup cleanup (no progress beyond staleness threshold)",
        )
    )
    return result.rowcount or 0
```

- [ ] **Step 4: Run tests, verify pass**

Run: `docker compose exec tvbf-backend pytest tests/test_runs.py -v`
Expected: 6 passed.

- [ ] **Step 5: Task complete**

---

### Task 19: Startup cleanup of dangling runs

**Files:**
- Modify: `tvbf-backend/src/tvbf/main.py`
- Create: `tvbf-backend/tests/test_startup_cleanup.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_startup_cleanup.py`:

```python
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from tvbf.main import run_startup_cleanup
from tvbf.tvmaze import models as m


async def test_startup_cleanup_cancels_stale_running_runs(session):
    stale = m.IngestRun(kind="initial", status="running")
    stale.last_progress_at = datetime.now(timezone.utc) - timedelta(hours=2)
    session.add(stale)
    fresh = m.IngestRun(kind="initial", status="running")
    fresh.last_progress_at = datetime.now(timezone.utc)
    session.add(fresh)
    await session.commit()

    await run_startup_cleanup(session, stale_after_minutes=15)
    await session.commit()

    rows = (await session.execute(select(m.IngestRun))).scalars().all()
    by_status = {r.status: r for r in rows}
    assert "cancelled" in by_status
    assert "running" in by_status
```

- [ ] **Step 2: Run test, verify failure**

Run: `docker compose exec tvbf-backend pytest tests/test_startup_cleanup.py -v`
Expected: ImportError — `run_startup_cleanup` does not exist.

- [ ] **Step 3: Add startup cleanup + lifespan to main.py**

Replace `src/tvbf/main.py`:

```python
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.config import get_settings
from tvbf.db import SessionLocal
from tvbf.routers import health
from tvbf.tvmaze.runs import mark_stale_runs_cancelled


async def run_startup_cleanup(session: AsyncSession, stale_after_minutes: int) -> int:
    count = await mark_stale_runs_cancelled(session, stale_after_minutes=stale_after_minutes)
    return count


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    async with SessionLocal() as session:
        await run_startup_cleanup(session, stale_after_minutes=settings.ingest_stale_run_minutes)
        await session.commit()
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="tvbf-backend", lifespan=lifespan)
    app.include_router(health.router)
    return app


app = create_app()
```

- [ ] **Step 4: Run test, verify pass**

Run: `docker compose exec tvbf-backend pytest tests/test_startup_cleanup.py -v`
Expected: 1 passed.

- [ ] **Step 5: Task complete**

---

### Task 20: Initial ingestion orchestrator

**Files:**
- Create: `tvbf-backend/src/tvbf/tvmaze/ingest.py`
- Create: `tvbf-backend/tests/test_ingest.py`
- Create: `tvbf-backend/tests/fixtures/tvmaze/__init__.py`
- Create: `tvbf-backend/tests/fixtures/tvmaze/show_factory.py`

- [ ] **Step 1: Create the payload factory fixture**

Create `tests/fixtures/tvmaze/__init__.py` (empty).

Create `tests/fixtures/tvmaze/show_factory.py`:

```python
def make_show(show_id: int, updated: int, seasons: int = 1, episodes_per_season: int = 2) -> dict:
    seasons_list = [
        {"id": show_id * 1000 + s, "number": s, "name": f"S{s}", "episodeOrder": episodes_per_season}
        for s in range(1, seasons + 1)
    ]
    episodes_list = []
    counter = 0
    for s in range(1, seasons + 1):
        for n in range(1, episodes_per_season + 1):
            counter += 1
            episodes_list.append({
                "id": show_id * 10000 + counter,
                "season": s,
                "number": n,
                "name": f"S{s}E{n}",
            })
    return {
        "id": show_id,
        "name": f"Show {show_id}",
        "type": "Scripted",
        "updated": updated,
        "genres": ["Drama"],
        "network": None,
        "webChannel": None,
        "_embedded": {"seasons": seasons_list, "episodes": episodes_list},
    }
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_ingest.py`:

```python
import httpx
import respx
from sqlalchemy import select

from tests.fixtures.tvmaze.show_factory import make_show
from tvbf.tvmaze import models as m
from tvbf.tvmaze.client import TVMazeClient
from tvbf.tvmaze.ingest import run_initial_ingest
from tvbf.tvmaze.runs import create_run


@respx.mock
async def test_initial_ingest_inserts_all_shows(session):
    respx.get("https://api.tvmaze.com/updates/shows").mock(
        return_value=httpx.Response(200, json={"1": 100, "2": 200})
    )
    respx.get("https://api.tvmaze.com/shows/1").mock(
        return_value=httpx.Response(200, json=make_show(1, 100))
    )
    respx.get("https://api.tvmaze.com/shows/2").mock(
        return_value=httpx.Response(200, json=make_show(2, 200))
    )

    run_id = await create_run(session, kind="initial")
    await session.commit()

    async with TVMazeClient("https://api.tvmaze.com", rate_calls=50, rate_window=1, retry_base_delay=0.01) as c:
        result = await run_initial_ingest(session_factory=lambda: session, client=c, run_id=run_id)

    assert result.shows_processed == 2
    assert result.shows_failed == 0
    assert result.last_update_cursor == 200
    shows = (await session.execute(select(m.Show))).scalars().all()
    assert {s.id for s in shows} == {1, 2}


@respx.mock
async def test_initial_ingest_skips_already_present_shows(session):
    session.add(m.Show(id=1, name="pre", tvmaze_updated=999))
    await session.commit()

    respx.get("https://api.tvmaze.com/updates/shows").mock(
        return_value=httpx.Response(200, json={"1": 100, "2": 200})
    )
    show2 = respx.get("https://api.tvmaze.com/shows/2").mock(
        return_value=httpx.Response(200, json=make_show(2, 200))
    )
    show1 = respx.get("https://api.tvmaze.com/shows/1").mock(
        return_value=httpx.Response(200, json=make_show(1, 100))
    )

    run_id = await create_run(session, kind="initial")
    await session.commit()

    async with TVMazeClient("https://api.tvmaze.com", rate_calls=50, rate_window=1, retry_base_delay=0.01) as c:
        result = await run_initial_ingest(session_factory=lambda: session, client=c, run_id=run_id)

    assert result.shows_processed == 1
    assert show2.call_count == 1
    assert show1.call_count == 0  # already present


@respx.mock
async def test_initial_ingest_continues_past_per_show_failures(session):
    respx.get("https://api.tvmaze.com/updates/shows").mock(
        return_value=httpx.Response(200, json={"1": 100, "2": 200, "3": 300})
    )
    respx.get("https://api.tvmaze.com/shows/1").mock(return_value=httpx.Response(200, json=make_show(1, 100)))
    respx.get("https://api.tvmaze.com/shows/2").mock(return_value=httpx.Response(404))
    respx.get("https://api.tvmaze.com/shows/3").mock(return_value=httpx.Response(200, json=make_show(3, 300)))

    run_id = await create_run(session, kind="initial")
    await session.commit()

    async with TVMazeClient(
        "https://api.tvmaze.com", rate_calls=50, rate_window=1, retry_max_attempts=2, retry_base_delay=0.01
    ) as c:
        result = await run_initial_ingest(session_factory=lambda: session, client=c, run_id=run_id)

    assert result.shows_processed == 2
    assert result.shows_failed == 1
    shows = (await session.execute(select(m.Show))).scalars().all()
    assert {s.id for s in shows} == {1, 3}
```

Note: the production `run_initial_ingest` uses a session factory so each show gets its own transaction (per-show atomicity, per spec). For tests we pass `lambda: session` — a single shared session — because the test fixture already uses one session. The production factory will be wired in at the router layer (Task 23).

- [ ] **Step 3: Run tests, verify failure**

Run: `docker compose exec tvbf-backend pytest tests/test_ingest.py -v`
Expected: ImportError on `run_initial_ingest`.

- [ ] **Step 4: Implement ingest.py**

Create `src/tvbf/tvmaze/ingest.py`:

```python
import logging
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.tvmaze import models as m
from tvbf.tvmaze.client import TVMazeClient
from tvbf.tvmaze.runs import finalize_run, record_progress
from tvbf.tvmaze.schemas import TVMazeShow
from tvbf.tvmaze.upsert import upsert_show_payload

log = logging.getLogger(__name__)


@dataclass
class IngestResult:
    shows_processed: int
    shows_failed: int
    last_update_cursor: int | None


SessionFactory = Callable[[], AsyncSession]


@asynccontextmanager
async def _owned_session(session_factory: SessionFactory) -> AsyncIterator[AsyncSession]:
    """Yield a session we manage. In tests, the factory returns an existing session (we do not close it)."""
    produced = session_factory()
    if hasattr(produced, "__aenter__"):
        async with produced as s:
            yield s
    else:
        try:
            yield produced
        finally:
            pass  # caller (test) owns lifecycle


async def run_initial_ingest(
    *,
    session_factory: SessionFactory,
    client: TVMazeClient,
    run_id: UUID,
    failure_threshold: int = 10,
) -> IngestResult:
    async with _owned_session(session_factory) as s:
        updates = await client.get_show_updates()
        cursor = max(updates.values()) if updates else None
        existing = set((await s.execute(select(m.Show.id))).scalars().all())
        todo = sorted(set(updates) - existing)

    processed = 0
    failed = 0
    consecutive_failures = 0

    for show_id in todo:
        try:
            payload = await client.get_show(show_id)
        except httpx.HTTPStatusError as e:
            log.warning("skipping show %d after http error: %s", show_id, e)
            failed += 1
            consecutive_failures += 1
            async with _owned_session(session_factory) as s:
                await record_progress(s, run_id, failed_delta=1)
                await s.commit()
            if consecutive_failures >= failure_threshold:
                async with _owned_session(session_factory) as s:
                    await finalize_run(
                        s, run_id, status="failed",
                        error=f"aborted after {consecutive_failures} consecutive failures",
                    )
                    await s.commit()
                return IngestResult(processed, failed, cursor)
            continue
        except Exception as e:
            log.exception("unexpected error for show %d", show_id)
            failed += 1
            consecutive_failures += 1
            async with _owned_session(session_factory) as s:
                await record_progress(s, run_id, failed_delta=1)
                await s.commit()
            if consecutive_failures >= failure_threshold:
                async with _owned_session(session_factory) as s:
                    await finalize_run(
                        s, run_id, status="failed",
                        error=f"aborted after {consecutive_failures} consecutive failures: {e}",
                    )
                    await s.commit()
                return IngestResult(processed, failed, cursor)
            continue

        try:
            async with _owned_session(session_factory) as s:
                show = TVMazeShow.model_validate(payload)
                await upsert_show_payload(s, show)
                await record_progress(s, run_id, processed_delta=1)
                await s.commit()
            processed += 1
            consecutive_failures = 0
        except Exception as e:
            log.exception("upsert failed for show %d", show_id)
            failed += 1
            consecutive_failures += 1
            async with _owned_session(session_factory) as s:
                await record_progress(s, run_id, failed_delta=1)
                await s.commit()
            if consecutive_failures >= failure_threshold:
                async with _owned_session(session_factory) as s:
                    await finalize_run(
                        s, run_id, status="failed",
                        error=f"aborted after {consecutive_failures} consecutive failures: {e}",
                    )
                    await s.commit()
                return IngestResult(processed, failed, cursor)

    async with _owned_session(session_factory) as s:
        await finalize_run(s, run_id, status="succeeded", last_update_cursor=cursor)
        await s.commit()

    return IngestResult(processed, failed, cursor)
```

- [ ] **Step 5: Run tests, verify pass**

Run: `docker compose exec tvbf-backend pytest tests/test_ingest.py -v`
Expected: 3 passed.

- [ ] **Step 6: Task complete**

---

### Task 21: Daily update orchestrator

**Files:**
- Create: `tvbf-backend/src/tvbf/tvmaze/update.py`
- Create: `tvbf-backend/tests/test_update.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_update.py`:

```python
import httpx
import respx
from sqlalchemy import select

from tests.fixtures.tvmaze.show_factory import make_show
from tvbf.tvmaze import models as m
from tvbf.tvmaze.client import TVMazeClient
from tvbf.tvmaze.runs import create_run, finalize_run
from tvbf.tvmaze.update import run_update


@respx.mock
async def test_update_only_fetches_shows_past_cursor(session):
    # Seed a previous successful run with cursor=100 and one show already present.
    prior_run = await create_run(session, kind="initial")
    await session.commit()
    await finalize_run(session, prior_run, status="succeeded", last_update_cursor=100)
    session.add(m.Show(id=1, name="pre", tvmaze_updated=100))
    await session.commit()

    respx.get("https://api.tvmaze.com/updates/shows").mock(
        return_value=httpx.Response(200, json={"1": 100, "2": 150, "3": 200})
    )
    old = respx.get("https://api.tvmaze.com/shows/1").mock(return_value=httpx.Response(200, json=make_show(1, 100)))
    s2 = respx.get("https://api.tvmaze.com/shows/2").mock(return_value=httpx.Response(200, json=make_show(2, 150)))
    s3 = respx.get("https://api.tvmaze.com/shows/3").mock(return_value=httpx.Response(200, json=make_show(3, 200)))

    run_id = await create_run(session, kind="update")
    await session.commit()

    async with TVMazeClient("https://api.tvmaze.com", rate_calls=50, rate_window=1, retry_base_delay=0.01) as c:
        result = await run_update(session_factory=lambda: session, client=c, run_id=run_id)

    assert result.shows_processed == 2
    assert result.last_update_cursor == 200
    assert old.call_count == 0
    assert s2.call_count == 1
    assert s3.call_count == 1


@respx.mock
async def test_update_with_no_prior_run_treats_cursor_as_zero(session):
    respx.get("https://api.tvmaze.com/updates/shows").mock(
        return_value=httpx.Response(200, json={"1": 10})
    )
    respx.get("https://api.tvmaze.com/shows/1").mock(return_value=httpx.Response(200, json=make_show(1, 10)))

    run_id = await create_run(session, kind="update")
    await session.commit()

    async with TVMazeClient("https://api.tvmaze.com", rate_calls=50, rate_window=1, retry_base_delay=0.01) as c:
        result = await run_update(session_factory=lambda: session, client=c, run_id=run_id)

    assert result.shows_processed == 1
    assert result.last_update_cursor == 10
```

- [ ] **Step 2: Run tests, verify failure**

Run: `docker compose exec tvbf-backend pytest tests/test_update.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement update.py**

Create `src/tvbf/tvmaze/update.py`:

```python
import logging
from uuid import UUID

import httpx

from tvbf.tvmaze.client import TVMazeClient
from tvbf.tvmaze.ingest import IngestResult, SessionFactory, _owned_session
from tvbf.tvmaze.runs import finalize_run, get_last_successful_cursor, record_progress
from tvbf.tvmaze.schemas import TVMazeShow
from tvbf.tvmaze.upsert import upsert_show_payload

log = logging.getLogger(__name__)


async def run_update(
    *,
    session_factory: SessionFactory,
    client: TVMazeClient,
    run_id: UUID,
    failure_threshold: int = 10,
) -> IngestResult:
    async with _owned_session(session_factory) as s:
        cursor = await get_last_successful_cursor(s) or 0

    updates = await client.get_show_updates()
    todo = sorted(sid for sid, epoch in updates.items() if epoch > cursor)
    max_epoch = max((updates[sid] for sid in todo), default=cursor)

    processed = 0
    failed = 0
    consecutive_failures = 0

    for show_id in todo:
        try:
            payload = await client.get_show(show_id)
        except httpx.HTTPStatusError as e:
            log.warning("skipping show %d after http error: %s", show_id, e)
            failed += 1
            consecutive_failures += 1
            async with _owned_session(session_factory) as s:
                await record_progress(s, run_id, failed_delta=1)
                await s.commit()
            if consecutive_failures >= failure_threshold:
                async with _owned_session(session_factory) as s:
                    await finalize_run(
                        s, run_id, status="failed",
                        error=f"aborted after {consecutive_failures} consecutive failures",
                    )
                    await s.commit()
                return IngestResult(processed, failed, cursor)
            continue

        try:
            async with _owned_session(session_factory) as s:
                await upsert_show_payload(s, TVMazeShow.model_validate(payload))
                await record_progress(s, run_id, processed_delta=1)
                await s.commit()
            processed += 1
            consecutive_failures = 0
        except Exception as e:
            log.exception("upsert failed for show %d", show_id)
            failed += 1
            consecutive_failures += 1
            async with _owned_session(session_factory) as s:
                await record_progress(s, run_id, failed_delta=1)
                await s.commit()
            if consecutive_failures >= failure_threshold:
                async with _owned_session(session_factory) as s:
                    await finalize_run(
                        s, run_id, status="failed",
                        error=f"aborted after {consecutive_failures} consecutive failures: {e}",
                    )
                    await s.commit()
                return IngestResult(processed, failed, cursor)

    async with _owned_session(session_factory) as s:
        await finalize_run(s, run_id, status="succeeded", last_update_cursor=max_epoch)
        await s.commit()

    return IngestResult(processed, failed, max_epoch)
```

- [ ] **Step 4: Run tests, verify pass**

Run: `docker compose exec tvbf-backend pytest tests/test_update.py -v`
Expected: 2 passed.

- [ ] **Step 5: Task complete**

---

### Task 22: Admin auth dependency

**Files:**
- Create: `tvbf-backend/src/tvbf/deps.py`
- Create: `tvbf-backend/tests/test_auth.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_auth.py`:

```python
from fastapi import FastAPI, Depends
from fastapi.testclient import TestClient

from tvbf.deps import require_admin


def build_client(monkeypatch) -> TestClient:
    monkeypatch.setenv("ADMIN_TOKEN", "shh")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x/y")
    # Clear lru_cache so new env is picked up.
    from tvbf.config import get_settings
    get_settings.cache_clear()

    app = FastAPI()

    @app.get("/secret", dependencies=[Depends(require_admin)])
    async def secret():
        return {"ok": True}

    return TestClient(app)


def test_require_admin_rejects_missing_header(monkeypatch):
    client = build_client(monkeypatch)
    r = client.get("/secret")
    assert r.status_code == 401


def test_require_admin_rejects_wrong_token(monkeypatch):
    client = build_client(monkeypatch)
    r = client.get("/secret", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_require_admin_accepts_correct_token(monkeypatch):
    client = build_client(monkeypatch)
    r = client.get("/secret", headers={"Authorization": "Bearer shh"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}
```

- [ ] **Step 2: Run tests, verify failure**

Run: `docker compose exec tvbf-backend pytest tests/test_auth.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement deps.py**

Create `src/tvbf/deps.py`:

```python
from collections.abc import AsyncIterator

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.config import Settings, get_settings
from tvbf.db import SessionLocal


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


def require_admin(
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    if token != settings.admin_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token")
```

- [ ] **Step 4: Run tests, verify pass**

Run: `docker compose exec tvbf-backend pytest tests/test_auth.py -v`
Expected: 3 passed.

- [ ] **Step 5: Task complete**

---

### Task 23: Admin routes

**Files:**
- Create: `tvbf-backend/src/tvbf/routers/admin.py`
- Modify: `tvbf-backend/src/tvbf/main.py`
- Create: `tvbf-backend/tests/test_admin_routes.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_admin_routes.py`:

```python
import asyncio
import os

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.fixtures.tvmaze.show_factory import make_show
from tvbf.main import app
from tvbf.tvmaze import models as m


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "shh")
    from tvbf.config import get_settings
    get_settings.cache_clear()
    return TestClient(app)


def test_ingest_rejects_unauth(client):
    r = client.post("/admin/ingest")
    assert r.status_code == 401


@respx.mock
def test_update_runs_synchronously(client, session):
    respx.get("https://api.tvmaze.com/updates/shows").mock(
        return_value=httpx.Response(200, json={"1": 10})
    )
    respx.get("https://api.tvmaze.com/shows/1").mock(
        return_value=httpx.Response(200, json=make_show(1, 10))
    )
    r = client.post("/admin/update", headers={"Authorization": "Bearer shh"})
    assert r.status_code == 200
    body = r.json()
    assert body["shows_processed"] == 1
    assert body["last_update_cursor"] == 10


def test_ingest_accepts_and_returns_run_id(client, session):
    # Short-circuit by mocking the client so the background task finishes fast.
    with respx.mock(base_url="https://api.tvmaze.com") as m_api:
        m_api.get("/updates/shows").mock(return_value=httpx.Response(200, json={}))
        r = client.post("/admin/ingest", headers={"Authorization": "Bearer shh"})
    assert r.status_code == 202
    assert "run_id" in r.json()


def test_ingest_status_endpoint(client, session):
    with respx.mock(base_url="https://api.tvmaze.com") as m_api:
        m_api.get("/updates/shows").mock(return_value=httpx.Response(200, json={}))
        r = client.post("/admin/ingest", headers={"Authorization": "Bearer shh"})
    run_id = r.json()["run_id"]
    # Give the background task a moment.
    asyncio.get_event_loop().run_until_complete(asyncio.sleep(0.2))
    status = client.get(f"/admin/ingest/{run_id}", headers={"Authorization": "Bearer shh"})
    assert status.status_code == 200
    assert status.json()["id"] == run_id
```

Note: these tests use the production app against the real `tvbf_test` database (via the session fixture's engine). The session fixture already points at `TEST_DATABASE_URL`; the app reads `DATABASE_URL`. For the tests to hit the same DB, set `DATABASE_URL=TEST_DATABASE_URL` at the top of `conftest.py`:

Modify `tests/conftest.py` — at the top, before any imports from `tvbf`:

```python
import os
os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
```

- [ ] **Step 2: Implement admin.py**

Create `src/tvbf/routers/admin.py`:

```python
import asyncio
import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.config import Settings, get_settings
from tvbf.db import SessionLocal
from tvbf.deps import get_session, require_admin
from tvbf.tvmaze import models as m
from tvbf.tvmaze.client import TVMazeClient
from tvbf.tvmaze.ingest import run_initial_ingest
from tvbf.tvmaze.runs import create_run, finalize_run
from tvbf.tvmaze.update import run_update

log = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


def _session_factory():
    return SessionLocal()


async def _background_ingest(run_id: UUID, settings: Settings) -> None:
    try:
        async with TVMazeClient(
            base_url=settings.tvmaze_base_url,
            rate_calls=settings.tvmaze_rate_limit_requests,
            rate_window=settings.tvmaze_rate_limit_window_seconds,
            retry_max_attempts=settings.tvmaze_retry_max_attempts,
        ) as client:
            await run_initial_ingest(
                session_factory=_session_factory,
                client=client,
                run_id=run_id,
                failure_threshold=settings.ingest_consecutive_failure_threshold,
            )
    except Exception as e:
        log.exception("background ingest crashed")
        async with SessionLocal() as s:
            await finalize_run(s, run_id, status="failed", error=str(e))
            await s.commit()


@router.post("/ingest", status_code=status.HTTP_202_ACCEPTED)
async def trigger_ingest(
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    run_id = await create_run(session, kind="initial")
    await session.commit()
    asyncio.create_task(_background_ingest(run_id, settings))
    return {"run_id": str(run_id)}


@router.post("/update")
async def trigger_update(
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> dict:
    run_id = await create_run(session, kind="update")
    await session.commit()

    async with TVMazeClient(
        base_url=settings.tvmaze_base_url,
        rate_calls=settings.tvmaze_rate_limit_requests,
        rate_window=settings.tvmaze_rate_limit_window_seconds,
        retry_max_attempts=settings.tvmaze_retry_max_attempts,
    ) as client:
        result = await run_update(
            session_factory=_session_factory,
            client=client,
            run_id=run_id,
            failure_threshold=settings.ingest_consecutive_failure_threshold,
        )
    return {
        "run_id": str(run_id),
        "shows_processed": result.shows_processed,
        "shows_failed": result.shows_failed,
        "last_update_cursor": result.last_update_cursor,
    }


@router.get("/ingest/{run_id}")
async def get_run_status(
    run_id: UUID, session: AsyncSession = Depends(get_session)
) -> dict:
    from sqlalchemy import select as _select
    row = (await session.execute(_select(m.IngestRun).where(m.IngestRun.id == run_id))).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="run not found")
    return {
        "id": str(row.id),
        "kind": row.kind,
        "status": row.status,
        "started_at": row.started_at.isoformat(),
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "shows_processed": row.shows_processed,
        "shows_failed": row.shows_failed,
        "last_update_cursor": row.last_update_cursor,
        "error": row.error,
    }
```

- [ ] **Step 3: Wire admin router into main.py**

Modify `src/tvbf/main.py`: change `create_app()` to include the admin router.

```python
from tvbf.routers import admin, health

# ...in create_app():
    app.include_router(health.router)
    app.include_router(admin.router)
```

- [ ] **Step 4: Run tests, verify pass**

Run: `docker compose exec tvbf-backend pytest tests/test_admin_routes.py -v`
Expected: 4 passed.

- [ ] **Step 5: Run the full test suite**

Run: `docker compose exec tvbf-backend pytest -v`
Expected: all tests from all files pass.

- [ ] **Step 6: Task complete**

---

### Task 24: Operational Taskfile targets

**Files:**
- Modify: `tvbf-backend/Taskfile.yml`

- [ ] **Step 1: Append operational targets**

```yaml
  test:
    desc: Run the pytest suite inside the container
    cmds:
      - "{{.EXEC}} pytest {{.CLI_ARGS}}"

  lint:
    desc: Run ruff check
    cmds:
      - "{{.EXEC}} ruff check src tests"

  format:
    desc: Run ruff format
    cmds:
      - "{{.EXEC}} ruff format src tests"

  ingest:
    desc: POST /admin/ingest
    cmds:
      - >
        curl -sk -X POST https://tvbf-backend.localhost/admin/ingest
        -H "Authorization: Bearer ${ADMIN_TOKEN:-dev-secret-change-me}"

  update:
    desc: POST /admin/update
    cmds:
      - >
        curl -sk -X POST https://tvbf-backend.localhost/admin/update
        -H "Authorization: Bearer ${ADMIN_TOKEN:-dev-secret-change-me}"

  ingest:status:
    desc: 'GET /admin/ingest/<id> — task ingest:status -- <uuid>'
    cmds:
      - >
        curl -sk https://tvbf-backend.localhost/admin/ingest/{{.CLI_ARGS}}
        -H "Authorization: Bearer ${ADMIN_TOKEN:-dev-secret-change-me}"

  deps:add:
    desc: 'Add a dependency and rebuild the image (task deps:add -- <package>)'
    cmds:
      - echo "Edit pyproject.toml to add {{.CLI_ARGS}}, then run task build."
```

- [ ] **Step 2: Verify targets list**

Run: `task -l`
Expected: lists all targets including `test`, `lint`, `format`, `ingest`, `update`.

- [ ] **Step 3: Smoke-test `task test`**

Run: `task test`
Expected: the full suite runs and passes.

- [ ] **Step 4: Smoke-test `task lint` and `task format`**

Run: `task lint`
Expected: exits clean (no errors; warnings acceptable).

Run: `task format`
Expected: formats files; re-running is a no-op.

- [ ] **Step 5: Task complete**

---

### Task 25: End-to-end smoke against the real TV Maze API

**Files:** none modified.

- [ ] **Step 1: Ensure the stack is up**

Run: `task infra:up` then `task up`.

- [ ] **Step 2: Trigger an update against the real API**

Note: on a completely empty database, `/admin/update` does nothing interesting. Instead, trigger `/admin/ingest` but stop it after a handful of shows by killing the container — this verifies the real path end-to-end without the ~8-hour full ingest.

Run: `task ingest`
Expected: returns `{"run_id": "..."}` with a 202.

Immediately follow with: `task ingest:status -- <run_id>`
Expected: status `running`, `shows_processed` growing.

- [ ] **Step 3: Let it run briefly then cancel**

Watch `task logs` for a minute or two. You should see successful fetches scrolling by at ~1.8 req/s. Then:

Run: `task down` (this kills the container mid-ingest).

- [ ] **Step 4: Verify resumability**

Run: `task up`.

Wait a few seconds for the lifespan cleanup to run. Check the run status:

`task ingest:status -- <run_id>`
Expected: status `cancelled` with the cleanup note (assuming `last_progress_at` staleness triggered; otherwise status may still be `running` — that's fine, the resumption case still works).

Run: `task ingest` again.
Expected: new run id, status 202. `shows_processed` starts fresh, but the diff-based resumption only fetches shows not already in the DB. Verify by peeking at the logs — you should see it picking up from further along.

- [ ] **Step 5: Trigger an update cycle**

After any run completes successfully:

Run: `task update`
Expected: completes synchronously; returns a JSON body with `shows_processed`, `last_update_cursor`, etc. On a freshly-ingested DB this is likely 0 shows.

- [ ] **Step 6: Task complete**

The subsystem is complete. The user can commit at any granularity they prefer; remind them after this task and do not run git commands yourself.

---

## Self-review (completed inline)

- **Spec coverage:** every section of the spec (D1–D6, architecture, module layout, data model including `last_progress_at`, data flow for both initial and update, rate limiting and retry, error handling, security via bearer token, configuration env vars, testing categories, deployment/Taskfile) is implemented by at least one task. The "Open questions" section of the spec (production scheduler choice, multi-stage Dockerfile, cast/crew, self-hosted images) is deliberately out of scope — no tasks, as intended.
- **Placeholder scan:** no TBD/TODO/vague steps remain. The "Open questions" references in Task 25 are scoping language, not implementation placeholders.
- **Type/name consistency:** `run_initial_ingest` / `run_update` / `IngestResult` / `SessionFactory` / `_owned_session` are referenced consistently across Tasks 20, 21, 23. `upsert_show_payload` / `upsert_show` / `upsert_season` / `upsert_episodes` / `upsert_network` / `upsert_web_channel` / `upsert_genre_by_name` names are stable across Tasks 13–17, 20, 21. Model class names (`Show`, `Season`, `Episode`, `Network`, `WebChannel`, `Genre`, `ShowGenre`, `IngestRun`) are consistent across models.py and all test files.
- **Ambiguity:** one remaining subtlety is the `_owned_session` helper. Production uses `SessionLocal()` which is an async context manager; tests pass `lambda: session` returning an already-open session that must not be closed. The helper handles both: it detects `__aenter__` presence and either enters the context or yields directly. This is explicit in the code comment and the test notes.
