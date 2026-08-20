# Browse API — Design

**Date:** 2026-04-19
**Status:** Approved (brainstorming)
**Scope:** A public, read-only HTTP API over the `tvmaze` schema that lets the future React SPA (and any other client) browse shows, seasons, and episodes. User accounts, watch tracking, friends, and recommendations are explicitly out of scope for this spec and live in later phases.

## Context

The TV Maze ingestion subsystem (see `2026-04-19-tvmaze-ingestion-design.md`) populates the `tvmaze` schema with ~80k shows, their seasons, and their episodes. Nothing in the app reads that data yet. This spec defines a minimal, coherent browse API that makes the catalog visible to clients and that the frontend can be built against without waiting for the user service. It intentionally leaves "trending," "recent," and "recommended" for later specs, since those need product decisions this spec does not make.

## Goals

1. Support the three core frontend pages: a browse/search page, a show detail page with embedded seasons, and an episode list per show.
2. Stay read-only. No mutations in this subsystem.
3. Be buildable against in parallel with the frontend once the ingest has partial data (the endpoints work the moment rows exist).
4. Fit cleanly under the existing FastAPI app with no new infrastructure (no cache server, no search engine).

## Non-goals

- User accounts, sessions, or any per-user data. Those land with the user-service spec.
- Discovery surfaces (trending, recent-additions, similar shows). Deferred to a later spec that has real user or ingest-freshness data to work with.
- Fuzzy search, typo tolerance, full-text ranking. V1 uses substring `ILIKE` and is iterated only if real traffic shows it failing.
- Rate limiting, response compression, advanced caching (ETags, stale-while-revalidate). Simple `Cache-Control` headers only.
- Episode-by-id lookup. No frontend flow needs it yet.

## Decisions

### D1. Fully public, no auth

Browse endpoints are unauthenticated. Catalog data is public information from TV Maze; nothing leaks by serving it anonymously. CORS is locked to the configured frontend origins as a soft boundary against casual third-party reuse. Per-user state (watchlist, progress) will be gated by the user service later, on separate endpoints against the `app` schema — not these.

### D2. MVP+ endpoint surface

Six endpoints total: list-and-detail for shows plus reference data for filter dropdowns. No discovery endpoints, no episode-by-id.

### D3. Offset pagination with caps

Standard `?page=N&per_page=P` with `per_page ≤ 100` and `page ≤ 1000`. Real browse flows almost never go deep — users filter first, then scroll a short result set. Cursor pagination is a non-breaking addition later if the deep-offset problem ever shows up in practice.

### D4. Seasons embedded in show detail; episodes not

Seasons are a small closed set per show (typically <20) so embedding them in `GET /shows/{id}` eliminates a round-trip on the detail page. Episodes are potentially thousands per show — always a separate call to `/shows/{id}/episodes`, optionally scoped by `?season=N`.

### D5. Cache-Control only, no ETags

`Cache-Control: public, max-age=300` on every browse response. The catalog updates at most daily, so five minutes of staleness is safe and makes client navigation feel fast. ETags add implementation surface and would need a per-resource version source (`show.tvmaze_updated` works for one show, but list endpoints have no single version). Defer until measured to matter.

## Architecture

A thin router delegating to a query layer. No new services, no caches beyond HTTP, no search engine.

```
src/tvbf/
  routers/
    browse.py           # six endpoints; parses query params, calls queries, maps to DTOs
  tvmaze/
    browse_queries.py   # SQLAlchemy query builders (filters, sort, pagination, counts)
    dto.py              # Pydantic response models (ShowSummary, ShowDetail, SeasonOut, ...)
```

The router stays thin on purpose so the query builders can be unit-tested against the test DB without going through the HTTP layer. DTOs live separately so they can evolve independently of how rows are assembled.

`main.py` gains a `CORSMiddleware` configured from a new `CORS_ALLOWED_ORIGINS` env var (comma-separated, default `https://tvbf.localhost` in localdev). Browse responses gain `Cache-Control: public, max-age=300` either via a FastAPI dependency that sets the header or a small `@router.middleware`-style decorator — implementation detail for the plan.

## Endpoint reference

### `GET /shows`

Paginated, searchable, filterable, sortable list of shows.

Query parameters (all optional):

| Param | Type | Notes |
|---|---|---|
| `search` | string | case-insensitive substring match on `show.name` (ILIKE `%value%`) |
| `status` | string | exact match on `show.status` (`Running`, `Ended`, `To Be Determined`, `In Development`, ...) |
| `genre` | string, repeatable | genre name; multiple values AND together (show must have all listed genres) |
| `network` | integer, repeatable | network id; multiple values OR together (show belongs to any listed network) |
| `language` | string | exact match on `show.language` |
| `type` | string | exact match on `show.type` |
| `sort` | string | one of `name`, `-name`, `premiered`, `-premiered`, `tvmaze_updated`, `-tvmaze_updated`. Default `name`. Unknown keys → 422. |
| `page` | integer | 1-indexed, default 1, max 1000 |
| `per_page` | integer | default 50, max 100 |

Response:

```json
{
  "items": [ /* ShowSummary */ ],
  "page": 1,
  "per_page": 50,
  "total": 12345,
  "total_pages": 247
}
```

`total` is a separate `COUNT(*)` over the filtered set, executed in the same request. One extra query is acceptable; client-side caching offsets the repeated cost during navigation.

### `GET /shows/{id}`

Returns `ShowDetail` (see below) including embedded seasons. 404 if the id is unknown.

### `GET /shows/{id}/seasons`

Returns the seasons array from `ShowDetail` as a standalone response (same shape as the embedded field). 404 if the show id is unknown. Useful when a client already has the show payload cached and only needs fresh seasons.

### `GET /shows/{id}/episodes`

Returns all episodes for the show as a flat array, ordered by `(season, number)`. Optional `?season=N` narrows to a single season. 404 if the show id is unknown. No pagination — episode count per show is bounded by the same batching that the ingest already tolerates.

### `GET /genres`

Returns all genres as a flat `[ {id, name} ]` array, ordered by `name`. No pagination (TV Maze has ~28 genres total).

### `GET /networks`

Returns all networks as a flat `[ {id, name, country_code, country_name, timezone} ]` array, ordered by `name`. No pagination (a few hundred rows). Web channels are not exposed as a separate endpoint; they're nested inside show/season payloads.

## Response shapes (Pydantic DTOs)

### `ShowSummary`

Lightweight row used in the `items` of `GET /shows`.

```
id                   int
name                 str
type                 str | null
status               str | null
language             str | null
premiered            date | null
ended                date | null
image_medium         str | null     # TV Maze CDN URL
image_original       str | null     # TV Maze CDN URL
network              NetworkRef | null
web_channel          NetworkRef | null
genres               list[str]       # flattened genre names
```

`NetworkRef`: `{ id: int, name: str }`.

### `ShowDetail`

Used by `GET /shows/{id}`. Superset of `ShowSummary`.

```
…all ShowSummary fields…
summary              str | null     # HTML, as returned by TV Maze
runtime              int | null
official_site        str | null
externals            ExternalsOut | null
tvmaze_updated       int            # epoch from TV Maze's `updated` field
seasons              list[SeasonOut]
```

`ExternalsOut`: `{ imdb: str | null, tvdb: int | null, tvrage: int | null }`.

### `SeasonOut`

```
id                   int
number               int
name                 str | null
episode_order        int | null
premiere_date        date | null
end_date             date | null
network              NetworkRef | null
web_channel          NetworkRef | null
image_medium         str | null
image_original       str | null
summary              str | null
```

### `EpisodeOut`

```
id                   int
show_id              int
season_id            int | null
season               int
number               int | null
name                 str | null
airdate              date | null
airtime              time | null
runtime              int | null
summary              str | null
image_medium         str | null
image_original       str | null
```

### `GenreOut`, `NetworkOut`

```
GenreOut:   { id: int, name: str }
NetworkOut: { id: int, name: str, country_code: str | null,
              country_name: str | null, timezone: str | null }
```

## Query layer

`browse_queries.py` exposes functions like:

- `async def list_shows(session, filters: ShowFilters, sort: str, page: int, per_page: int) -> tuple[list[Show], int]` — returns `(rows, total_count)`.
- `async def get_show_with_seasons(session, show_id: int) -> Show | None`.
- `async def get_show_seasons(session, show_id: int) -> list[Season]`.
- `async def get_show_episodes(session, show_id: int, season: int | None) -> list[Episode]`.
- `async def list_genres(session) -> list[Genre]`.
- `async def list_networks(session) -> list[Network]`.

`ShowFilters` is a small dataclass/Pydantic model carrying the parsed query params. Sort is mapped from the public string to a SQLAlchemy expression via a small whitelist dictionary (unknown keys raise, caught by the router and returned as 422).

Filter composition uses SQLAlchemy `select()` with `.where()` clauses accumulated from each non-None filter. Genre AND semantics is a `show.id IN (SELECT show_id FROM show_genre JOIN genre ON ... WHERE name = ?)` subquery per value, or a `GROUP BY show_id HAVING COUNT(DISTINCT genre_id) = N` shape — the plan picks one.

## Error handling

| Condition | Response |
|---|---|
| Show id not found on detail / seasons / episodes | `404 {"detail": "show not found"}` |
| Invalid query param (bad `sort` key, `per_page > 100`, `page < 1`, non-integer) | `422` with FastAPI's default Pydantic error body |
| Database unreachable | `503` via the existing `/readyz`-style exception handler (out of scope to implement in this spec; exceptions bubble up to FastAPI's default handler for now) |
| Empty result on `/shows` | `200` with `items: []` and `total: 0` |

## Caching and CORS

**Cache-Control.** A small FastAPI dependency attaches `Cache-Control: public, max-age=300` to every browse response. Applied via `response_class` customization or a response header override — the plan picks. Admin and health endpoints are unaffected.

**CORS.** `main.py` adds `CORSMiddleware(allow_origins=settings.cors_allowed_origins, allow_methods=["GET"], allow_headers=["Content-Type"])`. A new `CORS_ALLOWED_ORIGINS` config key (comma-separated) feeds it; default `https://tvbf.localhost` in localdev. Production value set via Container Apps secrets.

## Testing

All tests run inside the container via `task test`.

- **Unit (`tests/test_browse_queries.py`)**: each helper in `browse_queries.py` tested against the test DB with a small seeded fixture of shows/seasons/episodes/networks/genres. Covers filter composition (including the genre AND case), sort keys, pagination bounds, empty results.
- **Integration (`tests/test_browse_routes.py`)**: uses `httpx.AsyncClient(ASGITransport(app=app))` consistent with the existing admin-route tests. Covers one happy path per endpoint, 404 on unknown ids, 422 on bad sort / out-of-range pagination, filter composition through the HTTP layer, `Cache-Control` header presence, and CORS preflight for a non-matching origin.
- **Fixture seed** lives in `tests/fixtures/browse/seed.py` and inserts ~10 shows with 2–3 seasons each, enough to exercise all filters.
- No E2E against TV Maze is needed; the browse API doesn't call TV Maze.

## Performance notes

No indexes are added in v1 beyond what Alembic already emits for foreign keys and unique constraints. If real usage shows slow queries, the following are the first candidates (each a small future migration):

- `CREATE INDEX ... ON tvmaze.show USING gin (name gin_trgm_ops)` with the `pg_trgm` extension, if `ILIKE '%foo%'` on show name dominates latency.
- `CREATE INDEX ... ON tvmaze.show (premiered)` and `(tvmaze_updated)` for sorts at scale.
- `CREATE INDEX ... ON tvmaze.episode (show_id, season, number)` already exists from the ingestion migration.

## Module and file layout

```
src/tvbf/
  main.py                        # + CORSMiddleware wire-up from settings
  config.py                      # + CORS_ALLOWED_ORIGINS (list[str])
  routers/
    browse.py                    # six endpoints (all GET)
  tvmaze/
    browse_queries.py            # list_shows, get_show_with_seasons, ...
    dto.py                       # ShowSummary, ShowDetail, SeasonOut, EpisodeOut, GenreOut, NetworkOut, NetworkRef, ExternalsOut, ShowListPage, ShowFilters
tests/
  fixtures/browse/seed.py        # seeded catalog for browse tests
  test_browse_queries.py         # unit tests against the query layer
  test_browse_routes.py          # integration tests through ASGITransport
```

## Open questions (deferred)

- Discovery endpoints (`/shows/recent`, `/shows/trending`, similar-shows suggestions) — each needs product decisions (what signals drive "trending"? what counts as "recent": ingested-recently or premiered-recently?). Will land in follow-up specs, likely after the user service provides per-user history.
- Cursor pagination — additive change; do only if deep-offset becomes a measurable problem.
- Response compression and rate limiting — middleware-level additions when traffic justifies them.
- ETags and conditional GET — similarly additive.
