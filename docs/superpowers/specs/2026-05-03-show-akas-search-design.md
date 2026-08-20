# Show AKA Search — Design

**Date:** 2026-05-03
**Status:** Proposed
**Owner:** Tom

## Problem

TVMaze stores foreign-language shows under their native title (e.g., `東京リベンジャーズ`, `Naruto: Shippūden`). Our browse search performs `ILIKE '%term%'` against `tvmaze.show.name` only, so users searching `"tokyo revengers"` or `"naruto shippuden"` get zero results for shows that exist in the catalog. This is a discoverability blocker for any show whose primary name is non-Latin or differs significantly from a viewer's expected title.

TVMaze publishes alternate titles ("AKAs") per show via a separate endpoint, `GET https://api.tvmaze.com/shows/{id}/akas`. Each AKA has a name and a country (and sometimes language). These are not embedded in the main `/shows/{id}` payload and are not currently fetched by our ingest.

## Goal

Make foreign-language shows findable by their common English (and other) titles, without changing the displayed name or breaking the existing browse API contract.

## Non-goals

- **Localizing the displayed title.** Shows continue to render under their TVMaze primary `name`. A future "preferred language" user setting could surface AKAs in the UI; that's out of scope here.
- **AKA-aware sorting.** Sort keys (`name`, `premiered`, `tvmaze_updated`, etc.) keep operating on `show.name`. AKAs only widen the search index.
- **Per-AKA filtering.** No new query parameters; users can't restrict to "shows with English AKAs" or filter by country.

## Architecture

### New table: `tvmaze.show_aka`

```sql
CREATE TABLE tvmaze.show_aka (
    id            BIGSERIAL PRIMARY KEY,
    show_id       BIGINT NOT NULL REFERENCES tvmaze.show(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    country_code  TEXT,    -- ISO-3166-1 alpha-2, e.g. "US", "GB", "JP". NULL when TVMaze omits.
    country_name  TEXT,    -- redundant with country_code but TVMaze sends it; cheaper than a country lookup.
    language      TEXT     -- ISO-639-1, e.g. "en", "ja". Frequently NULL on TVMaze.
);

CREATE INDEX ix_show_aka_show_id ON tvmaze.show_aka(show_id);
CREATE INDEX ix_show_aka_name_trgm ON tvmaze.show_aka USING gin (name gin_trgm_ops);
```

The trigram index matches the existing `show.name` index (we should confirm one exists; if not, add one in the same migration). It keeps `ILIKE '%foo%'` queries fast as the AKA table grows. With ~80k shows averaging 3-5 AKAs each, that's roughly 240k–400k rows — small.

We use `ON DELETE CASCADE` so removing a show (rare; TVMaze sometimes does merges) doesn't leave orphan AKAs.

### Pydantic schema

`tvmaze/schemas.py`:

```python
class TVMazeAka(BaseModel):
    name: str
    country: dict | None = None  # TVMaze nests country as {code, name, timezone}; we extract.
    language: str | None = None
    model_config = ConfigDict(extra="ignore")

    @property
    def country_code(self) -> str | None:
        return (self.country or {}).get("code")

    @property
    def country_name(self) -> str | None:
        return (self.country or {}).get("name")
```

### Client and ingest

The TVMaze client gains one method:

```python
async def fetch_akas(self, show_id: int) -> list[TVMazeAka]: ...
```

Hits `/shows/{id}/akas`. Subject to the same rate limiter as `fetch_show`. A 404 is possible (shows that genuinely have no AKAs return `[]` not 404, but defensive 404 handling matches `fetch_show`).

`tvmaze/upsert.py` gains `upsert_akas(db, show_id, akas)`:
- Delete-then-insert (`DELETE FROM show_aka WHERE show_id = :id` then bulk insert). AKA lists are short and the table is small; this is simpler than per-row upserts and matches how we'd want updates to behave (TVMaze can remove AKAs).
- Wrapped in the same per-show transaction as the show/season/episode upserts.

`tvmaze/ingest.py` (initial ingest) and `tvmaze/update.py` (daily delta) each call `fetch_akas` immediately after `fetch_show`, before the transaction commits. One extra HTTP request per show. The rate limiter handles pacing.

### Browse query

`tvmaze/browse_queries.list_shows` currently filters with:

```python
if filters.search:
    stmt = stmt.where(Show.name.ilike(f"%{filters.search}%"))
```

This becomes:

```python
if filters.search:
    needle = f"%{filters.search}%"
    aka_match = (
        select(ShowAka.show_id)
        .where(ShowAka.name.ilike(needle))
    )
    stmt = stmt.where(or_(Show.name.ilike(needle), Show.id.in_(aka_match)))
```

The count query mirrors the same `WHERE`. No change to pagination, sorting, or the response model — the listed `ShowSummary` continues to use `show.name`.

We don't surface which AKA matched (the response stays clean). If we later want to show "matched as: Tokyo Revengers (US)" hints, we can add a `matched_aka` field to the response without breaking existing consumers.

## Migration & backfill

1. **Schema migration.** Adds `tvmaze.show_aka`, the indexes, and (if absent) the `pg_trgm` extension and trigram index on `show.name`. `task makemigration -- "add show_aka table"`.

2. **Backfill.** A new admin endpoint, `POST /admin/backfill-akas` (bearer-token guarded like `/admin/ingest`), that iterates every `tvmaze.show.id`, calls `fetch_akas`, and upserts. Resumable: it skips rows that already have at least one AKA *or* a recently-set sentinel. To detect "we tried but the show genuinely has no AKAs," we record an "akas-fetched-at" timestamp on `show` (new column `akas_synced_at TIMESTAMPTZ`) so we don't re-fetch every run.

   Status reporting via the same `ingest_run` table pattern: `kind='akas_backfill'`. Stale-run cleanup logic carries over.

3. **Daily updates.** `tvmaze/update.py` already calls `fetch_show` for each delta'd show; we tack on `fetch_akas` in the same path so AKAs stay current automatically.

4. **Cost estimate.** ~80k shows × 1 request × `18 req / 10 sec` ≈ 12.5 hours of wall-clock for the backfill. That's the same order of magnitude as the original ingest, but is a one-time event. Daily updates touch only delta'd shows (small).

## Modules touched

```
src/tvbf/
  tvmaze/
    schemas.py       — TVMazeAka model
    models.py        — ShowAka SQLAlchemy table; akas_synced_at on Show
    client.py        — fetch_akas
    upsert.py        — upsert_akas
    ingest.py        — call fetch_akas in the per-show loop
    update.py        — call fetch_akas in the daily loop
    browse_queries.py — extend list_shows count + page WHERE
  routers/
    admin.py         — POST /admin/backfill-akas, GET /admin/backfill-akas/{run_id}

alembic/versions/
  XXX_add_show_aka_table.py
```

## Tests

- **Unit**: `upsert_akas` round-trips and replaces existing AKA rows on re-call.
- **Unit**: `list_shows` returns shows that match by AKA only, by name only, by both, and dedupes when both match.
- **Integration**: ingest a fixture show with AKAs, assert `show_aka` rows exist; re-run ingest, assert no duplicates.
- **Integration**: search via `GET /shows?search=tokyo+revengers` returns the show whose primary name is `東京リベンジャーズ` and whose AKAs include `Tokyo Revengers`.

## Decisions

1. **Backfill is its own endpoint** (`POST /admin/backfill-akas`), independent of `/admin/ingest`. Lets us run it as a one-time backfill without rebuilding the catalog, and lets us re-run it later (e.g., after adding a new field on `show_aka`) without touching the show ingest path.
2. **AKAs are cached raw.** No lowercasing, no parenthetical stripping. The trigram GIN index on `show_aka.name` keeps `ILIKE '%foo%'` fast regardless of case, and any normalization we did at index time would be lossy for the future "English title as primary" feature, which needs the exact TVMaze string for display.

## Decisions deferred (not blocking)

1. **Language filter at search time.** We're storing `language` but not using it in the query. If foreign-language false positives become a problem we can layer a "prefer AKAs whose `language='en'`" hint via Accept-Language or a user setting. Easy to add later; YAGNI for now.
2. **Surfacing the matched AKA in search results.** The list response could include `matched_aka_name` so the UI can render "Tokyo Revengers" alongside the native `name`. Holding off until we observe whether users find the bare native-title-only result confusing.

## Forward-compatible: English-as-primary-title display

A planned follow-up feature will display the canonical English title as the primary on show pages, with the native title as a parenthetical:

> **Tokyo Revengers** (東京リベンジャーズ)

The `show_aka` data we're persisting in this spec is sufficient for that feature without further ingest work. The picker logic will be: prefer rows where `language = 'en'`; among those, prefer `country_code` from a fallback list (`'US' > 'GB' > 'CA' > 'AU' > others`); fall back to the original `show.name` if no English AKA exists. That logic lives in a future spec — flagged here to confirm the storage shape (`name`, `country_code`, `country_name`, `language`) covers it.
