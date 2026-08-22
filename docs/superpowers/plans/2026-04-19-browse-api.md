# Browse API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a public, read-only HTTP API over the `tvmaze` schema that exposes show/season/episode browse, detail, search, filter, sort, and pagination plus reference-data endpoints for genres and networks.

**Architecture:** Thin FastAPI router delegating to an async SQLAlchemy query layer. No new infrastructure (no cache server, no search engine). CORS middleware locked to configured frontend origins. Simple `Cache-Control: public, max-age=300` on every browse response.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy 2.x async + asyncpg, Pydantic v2, httpx (for tests via ASGITransport). No new dependencies.

**Spec reference:** `docs/superpowers/specs/2026-04-19-browse-api-design.md`

---

## Execution notes for the implementing engineer

- **The user handles all git operations.** After each task, stop at the final "task complete" step. Do NOT run `git add`, `git commit`, or any other state-changing git command. Read-only git commands (`status`, `log`, `diff`) are fine.
- **Everything runs inside the container.** Run pytest via `task test -- <args>` or `docker compose exec tvbf-backend pytest <args>`. There is no host Python.
- **Follow the existing async-test patterns.** The `session` fixture in `tests/conftest.py` provides a session scoped to `tvbf_test`. Route tests use `httpx.AsyncClient(transport=ASGITransport(app=app))` — exactly the pattern in `tests/test_admin_routes.py`. Do NOT use sync `TestClient` for any endpoint that touches the DB; it causes asyncpg event-loop mismatches.
- **Upsert-era quirks to preserve:**
  - After a write via raw `insert().on_conflict_do_update()`, subsequent ORM selects need `execution_options={"populate_existing": True}`. The browse API is read-only so you shouldn't need this — but tests that seed data then query may.
  - When tests seed `Show` + `Season` in the same transaction, `await session.flush()` between adds is required (no `relationship()` in the models).
- **Pyright gotcha:** FastAPI's `Depends(...)` as a default argument triggers B008 — already ignored globally in `pyproject.toml`. No per-line ignores needed.

## File map

```
tvbf-backend/
  src/tvbf/
    config.py                         # Modified: + cors_allowed_origins
    main.py                           # Modified: + CORSMiddleware, + browse router include
    routers/
      browse.py                       # Created: all 6 browse endpoints
    tvmaze/
      dto.py                          # Created: Pydantic response models
      browse_queries.py               # Created: query helpers (list_shows, get_show_with_seasons, ...)
  tests/
    fixtures/browse/__init__.py       # Created
    fixtures/browse/seed.py           # Created: seeds ~10 shows for browse tests
    test_browse_queries.py            # Created: unit tests for query helpers
    test_browse_routes.py             # Created: integration tests via ASGITransport
    test_browse_cors_and_cache.py     # Created: CORS preflight + Cache-Control header
    test_config.py                    # Modified: assert cors_allowed_origins default
    conftest.py                       # Modified: re-export seed fixture if helpful (optional)
  docker-compose.yml                  # Modified: + CORS_ALLOWED_ORIGINS env var
```

---

### Task 1: Config — add `cors_allowed_origins`

**Files:**
- Modify: `tvbf-backend/src/tvbf/config.py`
- Modify: `tvbf-backend/tests/test_config.py`
- Modify: `tvbf-backend/docker-compose.yml`

- [ ] **Step 1: Add the failing assertion to `test_settings_has_sensible_defaults`**

In `tests/test_config.py`, append to the body of `test_settings_has_sensible_defaults`:

```python
    assert s.cors_allowed_origins == ["https://tvbf.localhost"]
```

- [ ] **Step 2: Add a test that CORS_ALLOWED_ORIGINS env var is honored**

Append to `tests/test_config.py`:

```python
def test_settings_parses_cors_origins_from_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://a:b@c:5432/d")
    monkeypatch.setenv("ADMIN_TOKEN", "xxx")
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://a.example.com,https://b.example.com")
    s = Settings()  # type: ignore[call-arg]
    assert s.cors_allowed_origins == ["https://a.example.com", "https://b.example.com"]
```

- [ ] **Step 3: Run tests, verify failures**

Run: `task test -- tests/test_config.py -v`
Expected: one existing test fails on the new assertion; new test fails with AttributeError on `cors_allowed_origins`.

- [ ] **Step 4: Add the field to `Settings`**

In `src/tvbf/config.py`, add inside the `Settings` class (after `log_level`):

```python
    cors_allowed_origins_raw: str = Field(
        default="https://tvbf.localhost", alias="CORS_ALLOWED_ORIGINS"
    )

    @property
    def cors_allowed_origins(self) -> list[str]:
        return [o.strip() for o in self.cors_allowed_origins_raw.split(",") if o.strip()]
```

- [ ] **Step 5: Run tests, verify pass**

Run: `task test -- tests/test_config.py -v`
Expected: 4 passed.

- [ ] **Step 6: Wire the env var in `docker-compose.yml`**

In the `environment:` block of the `tvbf-backend` service, add below `LOG_LEVEL`:

```yaml
      CORS_ALLOWED_ORIGINS: "${CORS_ALLOWED_ORIGINS:-https://tvbf.localhost}"
```

- [ ] **Step 7: Task complete**

Do NOT commit. Move on.

---

### Task 2: CORS middleware

**Files:**
- Modify: `tvbf-backend/src/tvbf/main.py`
- Create: `tvbf-backend/tests/test_browse_cors_and_cache.py`

- [ ] **Step 1: Write a failing CORS preflight test**

Create `tests/test_browse_cors_and_cache.py`:

```python
import httpx
from httpx import ASGITransport

from tvbf.main import app


async def test_cors_allows_configured_origin():
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        r = await c.options(
            "/healthz",
            headers={
                "Origin": "https://tvbf.localhost",
                "Access-Control-Request-Method": "GET",
            },
        )
    assert r.status_code in (200, 204)
    assert r.headers.get("access-control-allow-origin") == "https://tvbf.localhost"


async def test_cors_blocks_unknown_origin():
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        r = await c.options(
            "/healthz",
            headers={
                "Origin": "https://attacker.example.com",
                "Access-Control-Request-Method": "GET",
            },
        )
    # Starlette's CORSMiddleware responds 400 to disallowed origins on preflight.
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers.keys()}
```

- [ ] **Step 2: Run tests, verify failure**

Run: `task test -- tests/test_browse_cors_and_cache.py -v`
Expected: assertions fail because no CORS middleware is registered.

- [ ] **Step 3: Register CORSMiddleware in `main.py`**

Replace `src/tvbf/main.py` with:

```python
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.config import get_settings
from tvbf.db import SessionLocal
from tvbf.routers import admin, health
from tvbf.tvmaze.runs import mark_stale_runs_cancelled


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        force=True,
    )


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
    settings = get_settings()
    _configure_logging(settings.log_level)
    app = FastAPI(title="tvbf-backend", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allowed_origins,
        allow_methods=["GET"],
        allow_headers=["Content-Type"],
    )
    app.include_router(health.router)
    app.include_router(admin.router)
    return app


app = create_app()
```

- [ ] **Step 4: Run CORS tests, verify pass**

Run: `task test -- tests/test_browse_cors_and_cache.py -v`
Expected: 2 passed.

- [ ] **Step 5: Run the full suite, verify no regressions**

Run: `task test`
Expected: all prior tests still pass plus the 2 new ones.

- [ ] **Step 6: Task complete**

---

### Task 3: `Cache-Control` dependency

**Files:**
- Create: `tvbf-backend/src/tvbf/routers/browse.py` (skeleton with dependency)
- Modify: `tvbf-backend/tests/test_browse_cors_and_cache.py`

- [ ] **Step 1: Add a failing test**

Append to `tests/test_browse_cors_and_cache.py`:

```python
async def test_browse_response_has_cache_control_header():
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        r = await c.get("/genres")
    assert r.status_code == 200
    assert r.headers.get("cache-control") == "public, max-age=300"
```

- [ ] **Step 2: Run tests, verify failure**

Run: `task test -- tests/test_browse_cors_and_cache.py -v`
Expected: fails with 404 on `/genres` (not implemented yet) AND the cache-control assertion fails.

- [ ] **Step 3: Create `routers/browse.py` with the dependency + a stub `/genres`**

Create `src/tvbf/routers/browse.py`:

```python
from fastapi import APIRouter, Response


def set_cache_control(response: Response) -> None:
    response.headers["Cache-Control"] = "public, max-age=300"


router = APIRouter(tags=["browse"])


@router.get("/genres")
async def list_genres(response: Response) -> list[dict]:
    set_cache_control(response)
    return []
```

Include it in `main.py`'s `create_app()`:

```python
from tvbf.routers import admin, browse, health
# ...
    app.include_router(browse.router)
```

- [ ] **Step 4: Run tests, verify pass**

Run: `task test -- tests/test_browse_cors_and_cache.py -v`
Expected: 3 passed (2 CORS + 1 cache header).

- [ ] **Step 5: Task complete**

---

### Task 4: DTOs (Pydantic response models)

**Files:**
- Create: `tvbf-backend/src/tvbf/tvmaze/dto.py`

- [ ] **Step 1: Write `dto.py` with all response models**

Create `src/tvbf/tvmaze/dto.py`:

```python
from datetime import date, time

from pydantic import BaseModel, ConfigDict


class NetworkRef(BaseModel):
    """Compact network/web-channel reference used inside shows and seasons."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str


class NetworkOut(BaseModel):
    """Full network representation for GET /networks."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    country_code: str | None = None
    country_name: str | None = None
    timezone: str | None = None


class GenreOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str


class ExternalsOut(BaseModel):
    imdb: str | None = None
    tvdb: int | None = None
    tvrage: int | None = None


class SeasonOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    number: int
    name: str | None = None
    episode_order: int | None = None
    premiere_date: date | None = None
    end_date: date | None = None
    network: NetworkRef | None = None
    web_channel: NetworkRef | None = None
    image_medium: str | None = None
    image_original: str | None = None
    summary: str | None = None


class EpisodeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    show_id: int
    season_id: int | None = None
    season: int
    number: int | None = None
    name: str | None = None
    airdate: date | None = None
    airtime: time | None = None
    runtime: int | None = None
    summary: str | None = None
    image_medium: str | None = None
    image_original: str | None = None


class ShowSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    type: str | None = None
    status: str | None = None
    language: str | None = None
    premiered: date | None = None
    ended: date | None = None
    image_medium: str | None = None
    image_original: str | None = None
    network: NetworkRef | None = None
    web_channel: NetworkRef | None = None
    genres: list[str] = []


class ShowDetail(ShowSummary):
    summary: str | None = None
    runtime: int | None = None
    official_site: str | None = None
    externals: ExternalsOut | None = None
    tvmaze_updated: int
    seasons: list[SeasonOut] = []


class ShowListPage(BaseModel):
    items: list[ShowSummary]
    page: int
    per_page: int
    total: int
    total_pages: int
```

- [ ] **Step 2: Verify the module parses**

Run: `docker compose exec -T tvbf-backend python -c "from tvbf.tvmaze.dto import ShowSummary, ShowDetail, SeasonOut, EpisodeOut, GenreOut, NetworkOut, NetworkRef, ExternalsOut, ShowListPage; print('ok')"`
Expected: `ok`.

- [ ] **Step 3: Task complete**

No unit tests here — DTOs are exercised through endpoint tests starting in Task 6.

---

### Task 5: Browse test seed fixture

**Files:**
- Create: `tvbf-backend/tests/fixtures/browse/__init__.py`
- Create: `tvbf-backend/tests/fixtures/browse/seed.py`

- [ ] **Step 1: Create the empty package init**

Create `tests/fixtures/browse/__init__.py` as an empty file.

- [ ] **Step 2: Create the seed module**

Create `tests/fixtures/browse/seed.py`:

```python
"""Seeded catalog for browse-API tests.

Produces 10 shows spanning the filter dimensions exercised by the tests:
- Running vs Ended vs "To Be Determined" statuses
- English vs Spanish language
- Scripted vs Reality type
- Single-genre vs multi-genre
- Network-only, web-channel-only, both, neither
- Premiered in 1990, 2010, and 2024
"""

from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.tvmaze import models as m

NETWORK_A_ID = 1
NETWORK_B_ID = 2
WEB_CHANNEL_ID = 100

# Genres — deterministic ids via order of insertion (serial).
GENRES = ["Drama", "Crime", "Comedy", "Reality", "Mystery"]


async def seed(session: AsyncSession) -> None:
    """Populate the test DB with a fixed catalog. Idempotency is NOT required;
    tests that use this fixture run against a fresh/truncated session."""

    # Networks
    session.add(m.Network(id=NETWORK_A_ID, name="Network A", country_code="US"))
    session.add(m.Network(id=NETWORK_B_ID, name="Network B", country_code="GB"))
    session.add(m.WebChannel(id=WEB_CHANNEL_ID, name="Web Channel X", country_code="US"))
    await session.flush()

    # Genres
    genre_id_by_name: dict[str, int] = {}
    for name in GENRES:
        g = m.Genre(name=name)
        session.add(g)
        await session.flush()
        genre_id_by_name[name] = g.id

    shows = [
        # (id, name, type, status, language, premiered, genres, network_id, web_channel_id)
        (1, "Running Drama",    "Scripted", "Running",            "English", date(2020, 1, 1),  ["Drama", "Crime"],                NETWORK_A_ID, None),
        (2, "Ended Drama",      "Scripted", "Ended",              "English", date(2012, 1, 1),  ["Drama"],                         NETWORK_A_ID, None),
        (3, "Running Comedy",   "Scripted", "Running",            "English", date(2019, 1, 1),  ["Comedy"],                        NETWORK_B_ID, None),
        (4, "Spanish Drama",    "Scripted", "Running",            "Spanish", date(2021, 1, 1),  ["Drama"],                         NETWORK_B_ID, None),
        (5, "Running Reality",  "Reality",  "Running",            "English", date(2018, 1, 1),  ["Reality"],                       NETWORK_A_ID, None),
        (6, "Ancient Show",     "Scripted", "Ended",              "English", date(1990, 1, 1),  ["Drama"],                         None,         None),
        (7, "New Show",         "Scripted", "Running",            "English", date(2024, 6, 1),  ["Comedy", "Drama"],               NETWORK_A_ID, None),
        (8, "Web Only",         "Scripted", "Running",            "English", date(2022, 1, 1),  ["Drama"],                         None,         WEB_CHANNEL_ID),
        (9, "Multi Genre",      "Scripted", "Running",            "English", date(2023, 1, 1),  ["Drama", "Crime", "Mystery"],     NETWORK_B_ID, None),
        (10, "TBD Show",        "Scripted", "To Be Determined",   "English", date(2025, 1, 1),  ["Drama"],                         NETWORK_A_ID, None),
    ]

    for i, (show_id, name, type_, status_, lang, premiered, genre_names, net, wc) in enumerate(shows):
        tvmaze_updated = 1_700_000_000 + show_id  # stable, increasing
        session.add(
            m.Show(
                id=show_id,
                name=name,
                type=type_,
                status=status_,
                language=lang,
                premiered=premiered,
                network_id=net,
                web_channel_id=wc,
                tvmaze_updated=tvmaze_updated,
            )
        )
        await session.flush()
        for genre_name in genre_names:
            session.add(m.ShowGenre(show_id=show_id, genre_id=genre_id_by_name[genre_name]))
        # Two seasons per show, two episodes per season, for episode-endpoint tests.
        for season_num in (1, 2):
            season_id = show_id * 100 + season_num
            session.add(
                m.Season(id=season_id, show_id=show_id, number=season_num, episode_order=2)
            )
            await session.flush()
            for ep_num in (1, 2):
                ep_id = show_id * 1000 + season_num * 10 + ep_num
                session.add(
                    m.Episode(
                        id=ep_id,
                        show_id=show_id,
                        season_id=season_id,
                        season=season_num,
                        number=ep_num,
                        name=f"{name} S{season_num}E{ep_num}",
                    )
                )

    await session.commit()
```

- [ ] **Step 3: Task complete**

The seed is called explicitly by tests via `await seed(session)`.

---

### Task 6: `GET /genres`

**Files:**
- Create: `tvbf-backend/src/tvbf/tvmaze/browse_queries.py`
- Modify: `tvbf-backend/src/tvbf/routers/browse.py`
- Create: `tvbf-backend/tests/test_browse_routes.py`
- Create: `tvbf-backend/tests/test_browse_queries.py`

- [ ] **Step 1: Write failing query-layer test**

Create `tests/test_browse_queries.py`:

```python
from tests.fixtures.browse.seed import GENRES, seed
from tvbf.tvmaze.browse_queries import list_genres


async def test_list_genres_returns_all_in_name_order(session):
    await seed(session)
    rows = await list_genres(session)
    assert [g.name for g in rows] == sorted(GENRES)
```

- [ ] **Step 2: Write failing route-layer test**

Create `tests/test_browse_routes.py`:

```python
import httpx
import pytest
from httpx import ASGITransport

from tests.fixtures.browse.seed import GENRES, seed
from tvbf.main import app


@pytest.fixture
async def client(session):
    """ASGI client whose test DB is the one `session` manages. Depending on
    `session` triggers its teardown truncate after each test."""
    await seed(session)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def test_get_genres_returns_flat_sorted_list(client):
    r = await client.get("/genres")
    assert r.status_code == 200
    body = r.json()
    assert [g["name"] for g in body] == sorted(GENRES)
    assert all("id" in g and "name" in g for g in body)
```

- [ ] **Step 3: Run tests, verify failures**

Run: `task test -- tests/test_browse_queries.py tests/test_browse_routes.py -v`
Expected: ImportError on `tvbf.tvmaze.browse_queries.list_genres`, and the route test returns the stub `[]` instead of real data.

- [ ] **Step 4: Implement the query**

Create `src/tvbf/tvmaze/browse_queries.py`:

```python
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.tvmaze import models as m


async def list_genres(session: AsyncSession) -> list[m.Genre]:
    result = await session.execute(select(m.Genre).order_by(m.Genre.name))
    return list(result.scalars().all())
```

- [ ] **Step 5: Wire the route**

Replace `src/tvbf/routers/browse.py`:

```python
from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.deps import get_session
from tvbf.tvmaze import browse_queries
from tvbf.tvmaze.dto import GenreOut


def set_cache_control(response: Response) -> None:
    response.headers["Cache-Control"] = "public, max-age=300"


router = APIRouter(tags=["browse"])


@router.get("/genres", response_model=list[GenreOut])
async def list_genres(
    response: Response, session: AsyncSession = Depends(get_session)
) -> list:
    set_cache_control(response)
    return await browse_queries.list_genres(session)
```

- [ ] **Step 6: Run tests, verify pass**

Run: `task test -- tests/test_browse_queries.py tests/test_browse_routes.py -v`
Expected: 2 passed.

- [ ] **Step 7: Run the full suite**

Run: `task test`
Expected: all prior + 2 new.

- [ ] **Step 8: Task complete**

---

### Task 7: `GET /networks`

**Files:**
- Modify: `tvbf-backend/src/tvbf/tvmaze/browse_queries.py`
- Modify: `tvbf-backend/src/tvbf/routers/browse.py`
- Modify: `tvbf-backend/tests/test_browse_queries.py`
- Modify: `tvbf-backend/tests/test_browse_routes.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_browse_queries.py`:

```python
from tvbf.tvmaze.browse_queries import list_networks


async def test_list_networks_returns_all_in_name_order(session):
    await seed(session)
    rows = await list_networks(session)
    assert [n.name for n in rows] == ["Network A", "Network B"]
```

Append to `tests/test_browse_routes.py`:

```python
async def test_get_networks_returns_flat_sorted_list(client):
    r = await client.get("/networks")
    assert r.status_code == 200
    body = r.json()
    assert [n["name"] for n in body] == ["Network A", "Network B"]
    # country fields included
    assert body[0]["country_code"] == "US"
    assert body[1]["country_code"] == "GB"
```

- [ ] **Step 2: Run tests, verify failure**

Run: `task test -- tests/test_browse_queries.py tests/test_browse_routes.py -v`
Expected: ImportError on `list_networks`; 404 on `/networks`.

- [ ] **Step 3: Append the query**

Append to `src/tvbf/tvmaze/browse_queries.py`:

```python
async def list_networks(session: AsyncSession) -> list[m.Network]:
    result = await session.execute(select(m.Network).order_by(m.Network.name))
    return list(result.scalars().all())
```

- [ ] **Step 4: Append the route**

In `src/tvbf/routers/browse.py`, add the import and the route:

```python
from tvbf.tvmaze.dto import GenreOut, NetworkOut
```

Append to the bottom of the file:

```python
@router.get("/networks", response_model=list[NetworkOut])
async def list_networks(
    response: Response, session: AsyncSession = Depends(get_session)
) -> list:
    set_cache_control(response)
    return await browse_queries.list_networks(session)
```

- [ ] **Step 5: Run tests, verify pass**

Run: `task test -- tests/test_browse_queries.py tests/test_browse_routes.py -v`
Expected: 4 passed (2 + 2).

- [ ] **Step 6: Task complete**

---

### Task 8: `GET /shows/{id}` (detail with embedded seasons)

**Files:**
- Modify: `tvbf-backend/src/tvbf/tvmaze/browse_queries.py`
- Modify: `tvbf-backend/src/tvbf/routers/browse.py`
- Modify: `tvbf-backend/tests/test_browse_queries.py`
- Modify: `tvbf-backend/tests/test_browse_routes.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_browse_queries.py`:

```python
from tvbf.tvmaze.browse_queries import get_show_with_seasons


async def test_get_show_with_seasons_returns_show_and_seasons(session):
    await seed(session)
    result = await get_show_with_seasons(session, 1)
    assert result is not None
    show, seasons, genres, network, web_channel = result
    assert show.name == "Running Drama"
    assert {g.name for g in genres} == {"Drama", "Crime"}
    assert network is not None and network.name == "Network A"
    assert web_channel is None
    assert sorted(s.number for s in seasons) == [1, 2]


async def test_get_show_with_seasons_returns_none_for_unknown_id(session):
    await seed(session)
    assert await get_show_with_seasons(session, 99999) is None
```

Append to `tests/test_browse_routes.py`:

```python
async def test_get_show_detail_includes_seasons_and_genres(client):
    r = await client.get("/shows/9")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "Multi Genre"
    assert set(body["genres"]) == {"Drama", "Crime", "Mystery"}
    assert body["network"] == {"id": 2, "name": "Network B"}
    assert body["web_channel"] is None
    assert sorted(s["number"] for s in body["seasons"]) == [1, 2]
    assert body["tvmaze_updated"] == 1_700_000_009


async def test_get_show_detail_returns_404_for_unknown_id(client):
    r = await client.get("/shows/99999")
    assert r.status_code == 404
    assert r.json()["detail"] == "show not found"
```

- [ ] **Step 2: Run tests, verify failure**

Run: `task test -- tests/test_browse_queries.py tests/test_browse_routes.py -v`
Expected: ImportError on `get_show_with_seasons`; 404 from default FastAPI (no detail) for /shows/9.

- [ ] **Step 3: Implement the query**

Append to `src/tvbf/tvmaze/browse_queries.py`:

```python
async def get_show_with_seasons(
    session: AsyncSession, show_id: int
) -> tuple[m.Show, list[m.Season], list[m.Genre], m.Network | None, m.WebChannel | None] | None:
    show = (
        await session.execute(select(m.Show).where(m.Show.id == show_id))
    ).scalar_one_or_none()
    if show is None:
        return None

    seasons = list(
        (
            await session.execute(
                select(m.Season).where(m.Season.show_id == show_id).order_by(m.Season.number)
            )
        )
        .scalars()
        .all()
    )

    genres = list(
        (
            await session.execute(
                select(m.Genre)
                .join(m.ShowGenre, m.ShowGenre.genre_id == m.Genre.id)
                .where(m.ShowGenre.show_id == show_id)
                .order_by(m.Genre.name)
            )
        )
        .scalars()
        .all()
    )

    network = None
    if show.network_id is not None:
        network = (
            await session.execute(select(m.Network).where(m.Network.id == show.network_id))
        ).scalar_one_or_none()

    web_channel = None
    if show.web_channel_id is not None:
        web_channel = (
            await session.execute(
                select(m.WebChannel).where(m.WebChannel.id == show.web_channel_id)
            )
        ).scalar_one_or_none()

    return show, seasons, genres, network, web_channel
```

- [ ] **Step 4: Add a DTO assembler**

Append to `src/tvbf/tvmaze/dto.py`:

```python
def build_show_detail(
    show, seasons, genres, network, web_channel
) -> "ShowDetail":
    def _season_network_refs(season):
        net = None
        wc = None
        if season.network_id is not None:
            # Seasons carry network_id/web_channel_id but we don't re-query them here;
            # the router is responsible for loading a name map if needed. In practice
            # season-level network references are uncommon; we leave them as None for
            # now and revisit in a follow-up when a UI actually displays them.
            pass
        return net, wc

    season_dtos: list[SeasonOut] = []
    for s in seasons:
        net_ref, wc_ref = _season_network_refs(s)
        season_dtos.append(
            SeasonOut(
                id=s.id,
                number=s.number,
                name=s.name,
                episode_order=s.episode_order,
                premiere_date=s.premiere_date,
                end_date=s.end_date,
                network=net_ref,
                web_channel=wc_ref,
                image_medium=s.image_medium,
                image_original=s.image_original,
                summary=s.summary,
            )
        )

    return ShowDetail(
        id=show.id,
        name=show.name,
        type=show.type,
        status=show.status,
        language=show.language,
        premiered=show.premiered,
        ended=show.ended,
        image_medium=show.image_medium,
        image_original=show.image_original,
        network=NetworkRef(id=network.id, name=network.name) if network else None,
        web_channel=NetworkRef(id=web_channel.id, name=web_channel.name)
        if web_channel
        else None,
        genres=[g.name for g in genres],
        summary=show.summary,
        runtime=show.runtime,
        official_site=show.official_site,
        externals=ExternalsOut(
            imdb=show.externals_imdb,
            tvdb=show.externals_tvdb,
            tvrage=show.externals_tvrage,
        )
        if (show.externals_imdb or show.externals_tvdb or show.externals_tvrage)
        else None,
        tvmaze_updated=show.tvmaze_updated,
        seasons=season_dtos,
    )
```

Note: season-level network/web-channel refs are intentionally left as `None` in v1. The `Season` rows store `network_id` / `web_channel_id` foreign keys, and a future refactor can populate `season.network` / `season.web_channel` from a batch lookup. The spec accepts this because no current UI consumes season-level networks.

- [ ] **Step 5: Wire the route**

In `src/tvbf/routers/browse.py`, add imports:

```python
from fastapi import APIRouter, Depends, HTTPException, Response
from tvbf.tvmaze.dto import GenreOut, NetworkOut, ShowDetail, build_show_detail
```

Append the route:

```python
@router.get("/shows/{show_id}", response_model=ShowDetail)
async def get_show(
    show_id: int,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> ShowDetail:
    set_cache_control(response)
    result = await browse_queries.get_show_with_seasons(session, show_id)
    if result is None:
        raise HTTPException(status_code=404, detail="show not found")
    show, seasons, genres, network, web_channel = result
    return build_show_detail(show, seasons, genres, network, web_channel)
```

- [ ] **Step 6: Run tests, verify pass**

Run: `task test -- tests/test_browse_queries.py tests/test_browse_routes.py -v`
Expected: 6 passed (4 prior + 2 new).

- [ ] **Step 7: Task complete**

---

### Task 9: `GET /shows/{id}/seasons`

**Files:**
- Modify: `tvbf-backend/src/tvbf/tvmaze/browse_queries.py`
- Modify: `tvbf-backend/src/tvbf/routers/browse.py`
- Modify: `tvbf-backend/tests/test_browse_queries.py`
- Modify: `tvbf-backend/tests/test_browse_routes.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_browse_queries.py`:

```python
from tvbf.tvmaze.browse_queries import get_show_seasons


async def test_get_show_seasons_returns_ordered_list(session):
    await seed(session)
    seasons = await get_show_seasons(session, 1)
    assert [s.number for s in seasons] == [1, 2]


async def test_get_show_seasons_returns_empty_for_unknown_id(session):
    await seed(session)
    assert await get_show_seasons(session, 99999) == []
```

Append to `tests/test_browse_routes.py`:

```python
async def test_get_show_seasons_endpoint_returns_seasons(client):
    r = await client.get("/shows/1/seasons")
    assert r.status_code == 200
    body = r.json()
    assert [s["number"] for s in body] == [1, 2]


async def test_get_show_seasons_endpoint_returns_404_for_unknown_show(client):
    r = await client.get("/shows/99999/seasons")
    assert r.status_code == 404
```

- [ ] **Step 2: Run tests, verify failure**

Run: `task test -- tests/test_browse_queries.py tests/test_browse_routes.py -v`
Expected: ImportError on `get_show_seasons`.

- [ ] **Step 3: Implement the query**

Append to `src/tvbf/tvmaze/browse_queries.py`:

```python
async def get_show_seasons(session: AsyncSession, show_id: int) -> list[m.Season]:
    result = await session.execute(
        select(m.Season).where(m.Season.show_id == show_id).order_by(m.Season.number)
    )
    return list(result.scalars().all())


async def show_exists(session: AsyncSession, show_id: int) -> bool:
    result = await session.execute(select(m.Show.id).where(m.Show.id == show_id))
    return result.scalar_one_or_none() is not None
```

- [ ] **Step 4: Wire the route**

In `src/tvbf/routers/browse.py`, add to imports:

```python
from tvbf.tvmaze.dto import GenreOut, NetworkOut, SeasonOut, ShowDetail, build_show_detail
```

Append:

```python
@router.get("/shows/{show_id}/seasons", response_model=list[SeasonOut])
async def get_show_seasons_route(
    show_id: int,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> list:
    set_cache_control(response)
    if not await browse_queries.show_exists(session, show_id):
        raise HTTPException(status_code=404, detail="show not found")
    return await browse_queries.get_show_seasons(session, show_id)
```

- [ ] **Step 5: Run tests, verify pass**

Run: `task test -- tests/test_browse_queries.py tests/test_browse_routes.py -v`
Expected: 10 passed (6 + 4).

- [ ] **Step 6: Task complete**

---

### Task 10: `GET /shows/{id}/episodes` (+ optional `?season=N`)

**Files:**
- Modify: `tvbf-backend/src/tvbf/tvmaze/browse_queries.py`
- Modify: `tvbf-backend/src/tvbf/routers/browse.py`
- Modify: `tvbf-backend/tests/test_browse_queries.py`
- Modify: `tvbf-backend/tests/test_browse_routes.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_browse_queries.py`:

```python
from tvbf.tvmaze.browse_queries import get_show_episodes


async def test_get_show_episodes_returns_all_by_default(session):
    await seed(session)
    eps = await get_show_episodes(session, 1, season=None)
    assert len(eps) == 4
    assert [(e.season, e.number) for e in eps] == [(1, 1), (1, 2), (2, 1), (2, 2)]


async def test_get_show_episodes_filters_by_season(session):
    await seed(session)
    eps = await get_show_episodes(session, 1, season=2)
    assert len(eps) == 2
    assert all(e.season == 2 for e in eps)
```

Append to `tests/test_browse_routes.py`:

```python
async def test_get_show_episodes_all(client):
    r = await client.get("/shows/1/episodes")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 4


async def test_get_show_episodes_filtered_by_season(client):
    r = await client.get("/shows/1/episodes?season=2")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 2
    assert all(e["season"] == 2 for e in body)


async def test_get_show_episodes_404_for_unknown_show(client):
    r = await client.get("/shows/99999/episodes")
    assert r.status_code == 404
```

- [ ] **Step 2: Run tests, verify failure**

Run: `task test -- tests/test_browse_queries.py tests/test_browse_routes.py -v`
Expected: ImportError on `get_show_episodes`.

- [ ] **Step 3: Implement the query**

Append to `src/tvbf/tvmaze/browse_queries.py`:

```python
async def get_show_episodes(
    session: AsyncSession, show_id: int, season: int | None
) -> list[m.Episode]:
    stmt = select(m.Episode).where(m.Episode.show_id == show_id)
    if season is not None:
        stmt = stmt.where(m.Episode.season == season)
    stmt = stmt.order_by(m.Episode.season, m.Episode.number)
    result = await session.execute(stmt)
    return list(result.scalars().all())
```

- [ ] **Step 4: Wire the route**

In `src/tvbf/routers/browse.py`, add to imports:

```python
from tvbf.tvmaze.dto import (
    EpisodeOut,
    GenreOut,
    NetworkOut,
    SeasonOut,
    ShowDetail,
    build_show_detail,
)
```

Append:

```python
@router.get("/shows/{show_id}/episodes", response_model=list[EpisodeOut])
async def get_show_episodes_route(
    show_id: int,
    response: Response,
    session: AsyncSession = Depends(get_session),
    season: int | None = None,
) -> list:
    set_cache_control(response)
    if not await browse_queries.show_exists(session, show_id):
        raise HTTPException(status_code=404, detail="show not found")
    return await browse_queries.get_show_episodes(session, show_id, season)
```

- [ ] **Step 5: Run tests, verify pass**

Run: `task test -- tests/test_browse_queries.py tests/test_browse_routes.py -v`
Expected: 15 passed (10 + 5).

- [ ] **Step 6: Task complete**

---

### Task 11: `GET /shows` — base list with pagination

**Files:**
- Modify: `tvbf-backend/src/tvbf/tvmaze/browse_queries.py`
- Modify: `tvbf-backend/src/tvbf/routers/browse.py`
- Modify: `tvbf-backend/src/tvbf/tvmaze/dto.py`
- Modify: `tvbf-backend/tests/test_browse_queries.py`
- Modify: `tvbf-backend/tests/test_browse_routes.py`

- [ ] **Step 1: Define the filter model in `dto.py`**

Append to `src/tvbf/tvmaze/dto.py`:

```python
from dataclasses import dataclass, field


ALLOWED_SORT_KEYS = {
    "name", "-name", "premiered", "-premiered", "tvmaze_updated", "-tvmaze_updated"
}


@dataclass
class ShowFilters:
    search: str | None = None
    status: str | None = None
    genres: list[str] = field(default_factory=list)
    network_ids: list[int] = field(default_factory=list)
    language: str | None = None
    type: str | None = None
```

- [ ] **Step 2: Add failing tests (base pagination only)**

Append to `tests/test_browse_queries.py`:

```python
from tvbf.tvmaze.browse_queries import list_shows
from tvbf.tvmaze.dto import ShowFilters


async def test_list_shows_returns_all_paginated_by_name(session):
    await seed(session)
    rows, total = await list_shows(session, ShowFilters(), sort="name", page=1, per_page=100)
    assert total == 10
    assert [s.name for s in rows] == sorted([
        "Ancient Show", "Ended Drama", "Multi Genre", "New Show",
        "Running Comedy", "Running Drama", "Running Reality", "Spanish Drama",
        "TBD Show", "Web Only",
    ])


async def test_list_shows_respects_page_boundaries(session):
    await seed(session)
    rows, total = await list_shows(session, ShowFilters(), sort="name", page=1, per_page=3)
    assert total == 10
    assert len(rows) == 3
    rows2, _ = await list_shows(session, ShowFilters(), sort="name", page=2, per_page=3)
    assert [r.id for r in rows] != [r.id for r in rows2]
```

Append to `tests/test_browse_routes.py`:

```python
async def test_get_shows_default_list(client):
    r = await client.get("/shows")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 10
    assert body["page"] == 1
    assert body["per_page"] == 50
    assert body["total_pages"] == 1
    assert len(body["items"]) == 10
    assert body["items"][0]["name"] == "Ancient Show"  # name asc


async def test_get_shows_pagination(client):
    r = await client.get("/shows?page=2&per_page=3")
    assert r.status_code == 200
    body = r.json()
    assert body["page"] == 2
    assert body["per_page"] == 3
    assert body["total"] == 10
    assert body["total_pages"] == 4
    assert len(body["items"]) == 3


async def test_get_shows_rejects_out_of_range_pagination(client):
    assert (await client.get("/shows?per_page=101")).status_code == 422
    assert (await client.get("/shows?page=0")).status_code == 422
```

- [ ] **Step 3: Run tests, verify failure**

Run: `task test -- tests/test_browse_queries.py tests/test_browse_routes.py -v`
Expected: ImportError on `list_shows` / 404 on `/shows`.

- [ ] **Step 4: Implement the query**

Append to `src/tvbf/tvmaze/browse_queries.py`:

```python
from sqlalchemy import func

from tvbf.tvmaze.dto import ALLOWED_SORT_KEYS, ShowFilters


_SORT_EXPRS = {
    "name": m.Show.name.asc(),
    "-name": m.Show.name.desc(),
    "premiered": m.Show.premiered.asc().nulls_last(),
    "-premiered": m.Show.premiered.desc().nulls_last(),
    "tvmaze_updated": m.Show.tvmaze_updated.asc(),
    "-tvmaze_updated": m.Show.tvmaze_updated.desc(),
}


async def list_shows(
    session: AsyncSession,
    filters: ShowFilters,
    sort: str,
    page: int,
    per_page: int,
) -> tuple[list[m.Show], int]:
    if sort not in ALLOWED_SORT_KEYS:
        raise ValueError(f"invalid sort key: {sort}")

    base = select(m.Show)
    # (Filters applied in later tasks.)

    total = (
        await session.execute(select(func.count()).select_from(base.subquery()))
    ).scalar_one()

    stmt = base.order_by(_SORT_EXPRS[sort], m.Show.id.asc()).limit(per_page).offset((page - 1) * per_page)
    rows = list((await session.execute(stmt)).scalars().all())
    return rows, total
```

- [ ] **Step 5: Add a summary-assembler helper to `dto.py`**

Append to `src/tvbf/tvmaze/dto.py`:

```python
def build_show_summary(
    show, genre_names: list[str], network: NetworkRef | None, web_channel: NetworkRef | None
) -> ShowSummary:
    return ShowSummary(
        id=show.id,
        name=show.name,
        type=show.type,
        status=show.status,
        language=show.language,
        premiered=show.premiered,
        ended=show.ended,
        image_medium=show.image_medium,
        image_original=show.image_original,
        network=network,
        web_channel=web_channel,
        genres=sorted(genre_names),
    )
```

- [ ] **Step 6: Implement the batch-hydration helper for the list route**

Append to `src/tvbf/tvmaze/browse_queries.py`:

```python
async def hydrate_show_refs(
    session: AsyncSession, shows: list[m.Show]
) -> tuple[
    dict[int, list[str]],             # show_id -> genre names
    dict[int, m.Network],              # network_id -> Network
    dict[int, m.WebChannel],           # web_channel_id -> WebChannel
]:
    if not shows:
        return {}, {}, {}

    show_ids = [s.id for s in shows]
    net_ids = {s.network_id for s in shows if s.network_id is not None}
    wc_ids = {s.web_channel_id for s in shows if s.web_channel_id is not None}

    genre_rows = (
        await session.execute(
            select(m.ShowGenre.show_id, m.Genre.name)
            .join(m.Genre, m.Genre.id == m.ShowGenre.genre_id)
            .where(m.ShowGenre.show_id.in_(show_ids))
        )
    ).all()
    genres_by_show: dict[int, list[str]] = {sid: [] for sid in show_ids}
    for sid, gname in genre_rows:
        genres_by_show[sid].append(gname)

    networks_by_id: dict[int, m.Network] = {}
    if net_ids:
        for row in (
            await session.execute(select(m.Network).where(m.Network.id.in_(net_ids)))
        ).scalars().all():
            networks_by_id[row.id] = row

    wcs_by_id: dict[int, m.WebChannel] = {}
    if wc_ids:
        for row in (
            await session.execute(select(m.WebChannel).where(m.WebChannel.id.in_(wc_ids)))
        ).scalars().all():
            wcs_by_id[row.id] = row

    return genres_by_show, networks_by_id, wcs_by_id
```

- [ ] **Step 7: Wire the route**

In `src/tvbf/routers/browse.py`, add to imports:

```python
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from tvbf.tvmaze.dto import (
    ALLOWED_SORT_KEYS,
    EpisodeOut,
    GenreOut,
    NetworkOut,
    NetworkRef,
    SeasonOut,
    ShowDetail,
    ShowFilters,
    ShowListPage,
    ShowSummary,
    build_show_detail,
    build_show_summary,
)
```

Append:

```python
@router.get("/shows", response_model=ShowListPage)
async def list_shows_route(
    response: Response,
    session: AsyncSession = Depends(get_session),
    search: str | None = None,
    status: str | None = None,
    genre: list[str] = Query(default_factory=list),
    network: list[int] = Query(default_factory=list),
    language: str | None = None,
    type: str | None = None,
    sort: str = "name",
    page: int = Query(default=1, ge=1, le=1000),
    per_page: int = Query(default=50, ge=1, le=100),
) -> ShowListPage:
    set_cache_control(response)

    if sort not in ALLOWED_SORT_KEYS:
        raise HTTPException(status_code=422, detail=f"invalid sort key: {sort}")

    filters = ShowFilters(
        search=search,
        status=status,
        genres=genre,
        network_ids=network,
        language=language,
        type=type,
    )
    rows, total = await browse_queries.list_shows(
        session, filters, sort=sort, page=page, per_page=per_page
    )
    genres_by_show, networks_by_id, wcs_by_id = await browse_queries.hydrate_show_refs(
        session, rows
    )

    items: list[ShowSummary] = []
    for show in rows:
        net = networks_by_id.get(show.network_id) if show.network_id is not None else None
        wc = wcs_by_id.get(show.web_channel_id) if show.web_channel_id is not None else None
        items.append(
            build_show_summary(
                show,
                genre_names=genres_by_show.get(show.id, []),
                network=NetworkRef(id=net.id, name=net.name) if net else None,
                web_channel=NetworkRef(id=wc.id, name=wc.name) if wc else None,
            )
        )

    import math

    return ShowListPage(
        items=items,
        page=page,
        per_page=per_page,
        total=total,
        total_pages=max(1, math.ceil(total / per_page)),
    )
```

- [ ] **Step 8: Run tests, verify pass**

Run: `task test -- tests/test_browse_queries.py tests/test_browse_routes.py -v`
Expected: 20 passed (15 + 5).

- [ ] **Step 9: Task complete**

---

### Task 12: `GET /shows` — simple filters (search, status, language, type)

**Files:**
- Modify: `tvbf-backend/src/tvbf/tvmaze/browse_queries.py`
- Modify: `tvbf-backend/tests/test_browse_queries.py`
- Modify: `tvbf-backend/tests/test_browse_routes.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_browse_queries.py`:

```python
async def test_list_shows_search_substring_case_insensitive(session):
    await seed(session)
    rows, total = await list_shows(
        session, ShowFilters(search="drama"), sort="name", page=1, per_page=100
    )
    names = {r.name for r in rows}
    assert "Running Drama" in names
    assert "Spanish Drama" in names
    assert "Running Comedy" not in names
    assert total == len(rows)


async def test_list_shows_status_filter(session):
    await seed(session)
    rows, _ = await list_shows(
        session, ShowFilters(status="Ended"), sort="name", page=1, per_page=100
    )
    assert {r.name for r in rows} == {"Ancient Show", "Ended Drama"}


async def test_list_shows_language_filter(session):
    await seed(session)
    rows, _ = await list_shows(
        session, ShowFilters(language="Spanish"), sort="name", page=1, per_page=100
    )
    assert {r.name for r in rows} == {"Spanish Drama"}


async def test_list_shows_type_filter(session):
    await seed(session)
    rows, _ = await list_shows(
        session, ShowFilters(type="Reality"), sort="name", page=1, per_page=100
    )
    assert {r.name for r in rows} == {"Running Reality"}
```

Append to `tests/test_browse_routes.py`:

```python
async def test_get_shows_search_substring(client):
    r = await client.get("/shows?search=drama")
    body = r.json()
    names = {i["name"] for i in body["items"]}
    assert "Running Drama" in names
    assert "Running Comedy" not in names


async def test_get_shows_status_filter(client):
    r = await client.get("/shows?status=Ended")
    assert {i["name"] for i in r.json()["items"]} == {"Ancient Show", "Ended Drama"}
```

- [ ] **Step 2: Run tests, verify failures**

Run: `task test -- tests/test_browse_queries.py tests/test_browse_routes.py -v`
Expected: new tests fail (filters not yet applied).

- [ ] **Step 3: Apply simple filters in `list_shows`**

Replace the body of `list_shows` in `src/tvbf/tvmaze/browse_queries.py`:

```python
async def list_shows(
    session: AsyncSession,
    filters: ShowFilters,
    sort: str,
    page: int,
    per_page: int,
) -> tuple[list[m.Show], int]:
    if sort not in ALLOWED_SORT_KEYS:
        raise ValueError(f"invalid sort key: {sort}")

    base = select(m.Show)
    if filters.search:
        base = base.where(m.Show.name.ilike(f"%{filters.search}%"))
    if filters.status is not None:
        base = base.where(m.Show.status == filters.status)
    if filters.language is not None:
        base = base.where(m.Show.language == filters.language)
    if filters.type is not None:
        base = base.where(m.Show.type == filters.type)

    total = (
        await session.execute(select(func.count()).select_from(base.subquery()))
    ).scalar_one()

    stmt = base.order_by(_SORT_EXPRS[sort], m.Show.id.asc()).limit(per_page).offset((page - 1) * per_page)
    rows = list((await session.execute(stmt)).scalars().all())
    return rows, total
```

- [ ] **Step 4: Run tests, verify pass**

Run: `task test -- tests/test_browse_queries.py tests/test_browse_routes.py -v`
Expected: 26 passed (20 + 6).

- [ ] **Step 5: Task complete**

---

### Task 13: `GET /shows` — genre filter (AND semantics)

**Files:**
- Modify: `tvbf-backend/src/tvbf/tvmaze/browse_queries.py`
- Modify: `tvbf-backend/tests/test_browse_queries.py`
- Modify: `tvbf-backend/tests/test_browse_routes.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_browse_queries.py`:

```python
async def test_list_shows_single_genre(session):
    await seed(session)
    rows, _ = await list_shows(
        session, ShowFilters(genres=["Crime"]), sort="name", page=1, per_page=100
    )
    assert {r.name for r in rows} == {"Multi Genre", "Running Drama"}


async def test_list_shows_multi_genre_and_semantics(session):
    await seed(session)
    rows, _ = await list_shows(
        session,
        ShowFilters(genres=["Drama", "Crime"]),
        sort="name",
        page=1,
        per_page=100,
    )
    # Only shows tagged with BOTH Drama AND Crime
    assert {r.name for r in rows} == {"Multi Genre", "Running Drama"}


async def test_list_shows_three_genre_and_semantics(session):
    await seed(session)
    rows, _ = await list_shows(
        session,
        ShowFilters(genres=["Drama", "Crime", "Mystery"]),
        sort="name",
        page=1,
        per_page=100,
    )
    assert {r.name for r in rows} == {"Multi Genre"}
```

Append to `tests/test_browse_routes.py`:

```python
async def test_get_shows_multi_genre(client):
    r = await client.get("/shows?genre=Drama&genre=Crime")
    assert {i["name"] for i in r.json()["items"]} == {"Multi Genre", "Running Drama"}
```

- [ ] **Step 2: Run tests, verify failures**

Run: `task test -- tests/test_browse_queries.py tests/test_browse_routes.py -v`
Expected: new tests fail (genre filter not applied).

- [ ] **Step 3: Implement the genre AND subquery**

In `src/tvbf/tvmaze/browse_queries.py`, add the genre-filter logic to `list_shows` — inside the existing body, right after the other simple filters but before the count query:

```python
    if filters.genres:
        genre_subq = (
            select(m.ShowGenre.show_id)
            .join(m.Genre, m.Genre.id == m.ShowGenre.genre_id)
            .where(m.Genre.name.in_(filters.genres))
            .group_by(m.ShowGenre.show_id)
            .having(func.count(func.distinct(m.Genre.id)) == len(filters.genres))
        )
        base = base.where(m.Show.id.in_(genre_subq))
```

The `HAVING COUNT(DISTINCT genre_id) = N` shape produces AND semantics without repeated subqueries.

- [ ] **Step 4: Run tests, verify pass**

Run: `task test -- tests/test_browse_queries.py tests/test_browse_routes.py -v`
Expected: 30 passed (26 + 4).

- [ ] **Step 5: Task complete**

---

### Task 14: `GET /shows` — network filter (OR semantics)

**Files:**
- Modify: `tvbf-backend/src/tvbf/tvmaze/browse_queries.py`
- Modify: `tvbf-backend/tests/test_browse_queries.py`
- Modify: `tvbf-backend/tests/test_browse_routes.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_browse_queries.py`:

```python
async def test_list_shows_single_network(session):
    await seed(session)
    rows, _ = await list_shows(
        session, ShowFilters(network_ids=[1]), sort="name", page=1, per_page=100
    )
    names = {r.name for r in rows}
    # Network A shows: Running Drama, Ended Drama, Running Reality, New Show, TBD Show
    assert names == {
        "Running Drama", "Ended Drama", "Running Reality", "New Show", "TBD Show"
    }


async def test_list_shows_multi_network_or_semantics(session):
    await seed(session)
    rows, _ = await list_shows(
        session, ShowFilters(network_ids=[1, 2]), sort="name", page=1, per_page=100
    )
    names = {r.name for r in rows}
    assert "Web Only" not in names  # has no network_id
    assert "Ancient Show" not in names  # has no network_id
    assert "Running Drama" in names  # Network A
    assert "Spanish Drama" in names  # Network B
```

Append to `tests/test_browse_routes.py`:

```python
async def test_get_shows_multi_network(client):
    r = await client.get("/shows?network=1&network=2")
    names = {i["name"] for i in r.json()["items"]}
    assert "Ancient Show" not in names
    assert "Web Only" not in names
    assert "Running Drama" in names
    assert "Spanish Drama" in names
```

- [ ] **Step 2: Run tests, verify failures**

Run: `task test -- tests/test_browse_queries.py tests/test_browse_routes.py -v`
Expected: new tests fail (network filter not applied).

- [ ] **Step 3: Add network filter**

In `src/tvbf/tvmaze/browse_queries.py`, add inside `list_shows` alongside the other filters:

```python
    if filters.network_ids:
        base = base.where(m.Show.network_id.in_(filters.network_ids))
```

- [ ] **Step 4: Run tests, verify pass**

Run: `task test -- tests/test_browse_queries.py tests/test_browse_routes.py -v`
Expected: 33 passed (30 + 3).

- [ ] **Step 5: Task complete**

---

### Task 15: `GET /shows` — sort

**Files:**
- Modify: `tvbf-backend/tests/test_browse_queries.py`
- Modify: `tvbf-backend/tests/test_browse_routes.py`

No code changes needed — `list_shows` already applies sort via `_SORT_EXPRS`. This task only adds tests to lock the behavior in.

- [ ] **Step 1: Add tests for each sort key**

Append to `tests/test_browse_queries.py`:

```python
async def test_list_shows_sort_name_desc(session):
    await seed(session)
    rows, _ = await list_shows(
        session, ShowFilters(), sort="-name", page=1, per_page=100
    )
    assert [r.name for r in rows[:2]] == ["Web Only", "TBD Show"]


async def test_list_shows_sort_premiered_desc(session):
    await seed(session)
    rows, _ = await list_shows(
        session, ShowFilters(), sort="-premiered", page=1, per_page=100
    )
    # TBD Show premiered 2025-01-01 (newest in seed).
    assert rows[0].name == "TBD Show"


async def test_list_shows_sort_tvmaze_updated_asc(session):
    await seed(session)
    rows, _ = await list_shows(
        session, ShowFilters(), sort="tvmaze_updated", page=1, per_page=100
    )
    # tvmaze_updated = 1_700_000_000 + show.id, so ascending is id ascending.
    assert [r.id for r in rows] == list(range(1, 11))


async def test_list_shows_invalid_sort_raises(session):
    await seed(session)
    try:
        await list_shows(session, ShowFilters(), sort="popularity", page=1, per_page=100)
    except ValueError as e:
        assert "popularity" in str(e)
    else:
        raise AssertionError("expected ValueError")
```

Append to `tests/test_browse_routes.py`:

```python
async def test_get_shows_sort_invalid_returns_422(client):
    r = await client.get("/shows?sort=popularity")
    assert r.status_code == 422


async def test_get_shows_sort_premiered_desc(client):
    r = await client.get("/shows?sort=-premiered")
    items = r.json()["items"]
    assert items[0]["name"] == "TBD Show"
```

- [ ] **Step 2: Run tests, verify pass (no implementation needed)**

Run: `task test -- tests/test_browse_queries.py tests/test_browse_routes.py -v`
Expected: 39 passed (33 + 6).

- [ ] **Step 3: Task complete**

---

### Task 16: Final-suite smoke + manual verification

**Files:** none.

- [ ] **Step 1: Run the full suite**

Run: `task test`
Expected: all tests pass (prior 48 + new browse-API tests).

- [ ] **Step 2: Run lint and typecheck**

Run: `task lint`
Expected: `All checks passed!`

Run: `task typecheck`
Expected: `0 errors, 0 warnings, 0 informations`

- [ ] **Step 3: Restart the container so CORS middleware + the new router are active**

Run: `task down && task up`

Wait for the container to report healthy:

```bash
until [ "$(docker inspect --format='{{.State.Health.Status}}' tvbf_backend 2>/dev/null)" = "healthy" ]; do sleep 2; done
echo "ready"
```

- [ ] **Step 4: Manual spot-check against the running container**

These depend on real ingested data in `tvbf` (the live DB, not the test DB). If the ingest has only partial data the counts will differ but the endpoints should still respond cleanly.

```bash
# Genres
curl -sk https://tvbf-backend.localhost/genres | python3 -m json.tool | head -20

# Networks (first 5)
curl -sk https://tvbf-backend.localhost/networks | python3 -c "import sys,json; print(json.dumps(json.load(sys.stdin)[:5], indent=2))"

# Shows page 1 (50 rows)
curl -sk "https://tvbf-backend.localhost/shows?per_page=5" | python3 -m json.tool

# A show detail (pick an ingested id)
curl -sk https://tvbf-backend.localhost/shows/1 | python3 -m json.tool | head -40

# Its seasons
curl -sk https://tvbf-backend.localhost/shows/1/seasons | python3 -m json.tool | head -20

# Its episodes, filtered to one season
curl -sk "https://tvbf-backend.localhost/shows/1/episodes?season=1" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))"
```

Expected: every command returns 200 with well-formed JSON. `Cache-Control: public, max-age=300` header present:

```bash
curl -skI https://tvbf-backend.localhost/genres | grep -i cache-control
```

- [ ] **Step 5: Task complete**

---

## Self-review (completed inline)

**Spec coverage:** every endpoint (6) has a task. Pagination (cursor not used — offset, as specified), search/filters/sort, error cases (404/422), CORS, `Cache-Control` header, and DTO shapes all mapped to at least one task. Performance-index notes in the spec are explicitly deferred to measurement, not part of the plan.

**Placeholder scan:** no TBD/TODO/vague steps. The Season-level network/web-channel nested objects are intentionally left as `None` in v1 with a comment explaining why — this matches the spec's "defer until a UI uses it" stance.

**Type/name consistency:**
- `list_shows`, `list_genres`, `list_networks`, `get_show_with_seasons`, `get_show_seasons`, `get_show_episodes`, `show_exists`, `hydrate_show_refs` — names stable across tasks.
- DTO types `ShowSummary`, `ShowDetail`, `SeasonOut`, `EpisodeOut`, `GenreOut`, `NetworkOut`, `NetworkRef`, `ExternalsOut`, `ShowListPage`, `ShowFilters`, plus `build_show_summary`/`build_show_detail`/`ALLOWED_SORT_KEYS` — stable across Tasks 4, 8, 11.
- Router function names are internally consistent within `browse.py`.
- Fixture constants (`NETWORK_A_ID=1`, `NETWORK_B_ID=2`, `WEB_CHANNEL_ID=100`, `GENRES=[...]`) referenced from tests match their definitions.

**One caveat surfaced during review:** the `test_config.py` test file already has a `test_settings_has_sensible_defaults` test from the ingestion plan. Task 1 Step 1 appends an assertion to it — fine since the implementing engineer will read the existing file and locate the function.
