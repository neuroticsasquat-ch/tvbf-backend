# Show AKA Search Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make foreign-language shows findable in browse search by their TVMaze "AKA" (alternate) titles, without changing what's displayed.

**Architecture:** A new `tvmaze.show_aka` table stores every AKA TVMaze publishes per show. Ingest and daily-update each fetch `/shows/{id}/akas` per show and replace that show's AKA rows. `GET /shows?search=` widens its `ILIKE` to match either `show.name` or any `show_aka.name`, deduped by show. A new `POST /admin/backfill-akas` admin endpoint backfills the existing ~80k catalog independent of the main ingest path.

**Tech Stack:** FastAPI, async SQLAlchemy, asyncpg, Alembic, pytest, Pydantic v2. PostgreSQL `pg_trgm` extension powers fast `ILIKE` substring matching. All commands run inside the `tvbf_backend` container (see `tvbf-backend/CLAUDE.md` for the host setup); `task` targets wrap them.

**Spec:** `docs/superpowers/specs/2026-05-03-show-akas-search-design.md`

---

## File map

**Create:**
- `alembic/versions/<rev>_add_show_aka_and_pgtrgm.py` — migration adding `show_aka`, `Show.akas_synced_at`, `pg_trgm` extension, trigram indexes on `show.name` and `show_aka.name`, and `'akas_backfill'` to the `ingest_run.kind` check constraint.
- `src/tvbf/tvmaze/akas_backfill.py` — backfill orchestrator (per-show fetch + upsert, resume on `akas_synced_at IS NULL`, run-state reporting).
- `tests/integration/tvmaze/test_show_aka_upsert.py` — integration tests for `upsert_akas`.
- `tests/integration/tvmaze/test_akas_backfill.py` — integration tests for the backfill orchestrator.
- `tests/integration/test_admin_backfill_akas.py` — integration tests for the new admin endpoints.
- `tests/integration/test_browse_aka_search.py` — integration tests for AKA-aware search.
- `tests/unit/tvmaze/test_akas_schema.py` — unit tests for `TVMazeAka` schema parsing.

**Modify:**
- `src/tvbf/tvmaze/models.py` — add `ShowAka` ORM model, `akas_synced_at` column on `Show`, expand `IngestRun.kind` constraint to include `'akas_backfill'`.
- `src/tvbf/tvmaze/schemas.py` — add `TVMazeAka`.
- `src/tvbf/tvmaze/client.py` — add `get_akas(show_id)` method.
- `src/tvbf/tvmaze/upsert.py` — add `upsert_akas(session, show_id, akas)` and a helper to set `akas_synced_at` on `show`.
- `src/tvbf/tvmaze/ingest.py` — call `get_akas` + `upsert_akas` inside the per-show transaction; mark `akas_synced_at`.
- `src/tvbf/tvmaze/update.py` — same change for the daily-delta path.
- `src/tvbf/tvmaze/browse_queries.py` — extend `list_shows` so each search token matches `show.name` OR any `show_aka.name` for the show.
- `src/tvbf/routers/admin.py` — add `POST /admin/backfill-akas` and `GET /admin/backfill-akas/{run_id}` (mirror existing `/admin/ingest` pattern).
- `src/tvbf/main.py` — extend the lifespan stale-run cleanup to include `'akas_backfill'` runs (or generalize it).
- `tvbf-backend/Taskfile.yml` — add `task akas:backfill` and `task akas:backfill:status` targets (parallel to `task ingest`/`task ingest:status`).

---

## Task 1: DB migration — `show_aka` table, `akas_synced_at`, `pg_trgm`, ingest_run kind

**Files:**
- Create: `alembic/versions/<rev>_add_show_aka_and_pgtrgm.py`
- Reference (for naming conventions): existing migrations under `alembic/versions/`.

- [ ] **Step 1: Generate the empty migration file**

Run inside the container:
```bash
task makemigration -- "add show_aka and pg_trgm"
```
Expected: a new file `alembic/versions/<rev>_add_show_aka_and_pg_trgm.py` is created. The autogenerator may include extraneous diffs since we haven't modified the model yet — open the file and **delete the generated body** so we can write the migration explicitly. (We do the model changes in Task 2; doing the migration first lets us hand-write a clean migration.)

- [ ] **Step 2: Replace the migration body with the explicit ops**

Open the new file and replace `upgrade()` and `downgrade()`:

```python
from alembic import op
import sqlalchemy as sa


def upgrade() -> None:
    # 1. pg_trgm extension (idempotent).
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # 2. Trigram index on show.name for fast ILIKE substring matching.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_show_name_trgm "
        "ON tvmaze.show USING gin (name gin_trgm_ops)"
    )

    # 3. show.akas_synced_at — NULL means "not yet synced", set after a successful AKA upsert.
    op.add_column(
        "show",
        sa.Column("akas_synced_at", sa.DateTime(timezone=True), nullable=True),
        schema="tvmaze",
    )

    # 4. show_aka table.
    op.create_table(
        "show_aka",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "show_id",
            sa.BigInteger(),
            sa.ForeignKey("tvmaze.show.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("country_code", sa.Text(), nullable=True),
        sa.Column("country_name", sa.Text(), nullable=True),
        sa.Column("language", sa.Text(), nullable=True),
        schema="tvmaze",
    )
    op.create_index(
        "ix_show_aka_show_id",
        "show_aka",
        ["show_id"],
        schema="tvmaze",
    )
    op.execute(
        "CREATE INDEX ix_show_aka_name_trgm "
        "ON tvmaze.show_aka USING gin (name gin_trgm_ops)"
    )

    # 5. Expand ingest_run.kind to include 'akas_backfill'.
    op.execute("ALTER TABLE tvmaze.ingest_run DROP CONSTRAINT ck_ingest_run_kind")
    op.execute(
        "ALTER TABLE tvmaze.ingest_run ADD CONSTRAINT ck_ingest_run_kind "
        "CHECK (kind IN ('initial', 'update', 'akas_backfill'))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE tvmaze.ingest_run DROP CONSTRAINT ck_ingest_run_kind")
    op.execute(
        "ALTER TABLE tvmaze.ingest_run ADD CONSTRAINT ck_ingest_run_kind "
        "CHECK (kind IN ('initial', 'update'))"
    )

    op.drop_index("ix_show_aka_name_trgm", table_name="show_aka", schema="tvmaze")
    op.drop_index("ix_show_aka_show_id", table_name="show_aka", schema="tvmaze")
    op.drop_table("show_aka", schema="tvmaze")

    op.drop_column("show", "akas_synced_at", schema="tvmaze")

    op.drop_index("ix_show_name_trgm", table_name="show", schema="tvmaze")
    # Leave pg_trgm installed; other migrations may rely on it.
```

- [ ] **Step 3: Run the migration on the dev database**

```bash
task migrate
```
Expected: Alembic upgrades to head; output mentions the new revision. No errors.

- [ ] **Step 4: Verify schema in psql**

```bash
docker exec tbc_postgresql_db psql -U root -d tvbf -c "\d tvmaze.show_aka"
docker exec tbc_postgresql_db psql -U root -d tvbf -c "\d tvmaze.show" | grep akas_synced_at
docker exec tbc_postgresql_db psql -U root -d tvbf -c "\dx pg_trgm"
docker exec tbc_postgresql_db psql -U root -d tvbf -c "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'ck_ingest_run_kind'"
```
Expected: `show_aka` exists with the columns above; `show.akas_synced_at` column exists; `pg_trgm` is installed; the kind constraint includes `'akas_backfill'`.

- [ ] **Step 5: Run the same migration against the test database**

The test DB (`tvbf_test`) is migrated by the test fixtures, but to be safe:
```bash
docker compose exec -T tvbf-backend alembic -x db=test upgrade head
```
If the project doesn't use the `-x db=test` switch, run `task test -- tests/integration -k schema` (Task 2 will rely on the test schema being current). If the conftest re-runs Alembic on session start, no manual migration needed.

---

## Task 2: ORM model — `ShowAka` + `Show.akas_synced_at` + ingest_run constraint

**Files:**
- Modify: `src/tvbf/tvmaze/models.py`

- [ ] **Step 1: Add `ShowAka` model and `akas_synced_at` column**

Open `src/tvbf/tvmaze/models.py`. After the existing `Show` class (around line 88) and before `class Season`, add `akas_synced_at` to `Show` and a new `ShowAka` class:

```python
# Inside class Show, just before the trailing blank line that ends the class:
    akas_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
```

Then below `Show`, add:

```python
class ShowAka(Base):
    __tablename__ = "show_aka"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    show_id: Mapped[int] = mapped_column(
        ForeignKey(f"{SCHEMA}.show.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    country_code: Mapped[str | None] = mapped_column(Text)
    country_name: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(Text)
```

- [ ] **Step 2: Update the `IngestRun.kind` CheckConstraint**

In the same file, update `IngestRun.__table_args__` from:

```python
        CheckConstraint("kind IN ('initial', 'update')", name="ck_ingest_run_kind"),
```

to:

```python
        CheckConstraint(
            "kind IN ('initial', 'update', 'akas_backfill')",
            name="ck_ingest_run_kind",
        ),
```

- [ ] **Step 3: Verify Alembic sees no diff**

```bash
docker compose exec -T tvbf-backend alembic check
```
Expected: "No new upgrade operations detected." If anything is reported, the migration in Task 1 doesn't match the ORM — fix the ORM (preferred) or update the migration. Then re-run `task migrate`.

- [ ] **Step 4: Run the existing test suite**

```bash
task test
```
Expected: 303 tests still pass. (No new tests yet — we're confirming the schema change didn't regress anything.)

---

## Task 3: Pydantic schema — `TVMazeAka`

**Files:**
- Create: `tests/unit/tvmaze/test_akas_schema.py`
- Modify: `src/tvbf/tvmaze/schemas.py`

- [ ] **Step 1: Write failing schema tests**

Create `tests/unit/tvmaze/test_akas_schema.py`:

```python
from tvbf.tvmaze.schemas import TVMazeAka


def test_parses_full_aka_payload():
    aka = TVMazeAka.model_validate(
        {
            "name": "Tokyo Revengers",
            "country": {"code": "US", "name": "United States", "timezone": "America/New_York"},
            "language": "en",
        }
    )
    assert aka.name == "Tokyo Revengers"
    assert aka.country_code == "US"
    assert aka.country_name == "United States"
    assert aka.language == "en"


def test_parses_aka_with_no_country_or_language():
    aka = TVMazeAka.model_validate({"name": "Альфа", "country": None})
    assert aka.name == "Альфа"
    assert aka.country_code is None
    assert aka.country_name is None
    assert aka.language is None


def test_ignores_extra_fields():
    aka = TVMazeAka.model_validate(
        {"name": "Foo", "country": None, "language": None, "_links": {"self": "http://x"}}
    )
    assert aka.name == "Foo"
```

- [ ] **Step 2: Run the tests, verify they fail**

```bash
task test -- tests/unit/tvmaze/test_akas_schema.py -v
```
Expected: ImportError or AttributeError because `TVMazeAka` does not exist yet.

- [ ] **Step 3: Add `TVMazeAka` to `schemas.py`**

Open `src/tvbf/tvmaze/schemas.py`. Add the new model near the other small DTOs (e.g., after `TVMazeNetwork`):

```python
class TVMazeAka(BaseModel):
    name: str
    country: dict | None = None
    language: str | None = None
    model_config = ConfigDict(extra="ignore")

    @property
    def country_code(self) -> str | None:
        return (self.country or {}).get("code")

    @property
    def country_name(self) -> str | None:
        return (self.country or {}).get("name")
```

(`BaseModel` and `ConfigDict` are already imported in this module.)

- [ ] **Step 4: Run the tests, verify they pass**

```bash
task test -- tests/unit/tvmaze/test_akas_schema.py -v
```
Expected: 3 tests pass.

---

## Task 4: TVMaze client — `get_akas`

**Files:**
- Modify: `src/tvbf/tvmaze/client.py`
- Test: extend `tests/unit/tvmaze/test_client.py` (existing).

- [ ] **Step 1: Locate the existing client tests**

Open `tests/unit/tvmaze/test_client.py` and read the existing `get_show` and `get_show_updates` tests to mirror the style (they typically use `respx` or `httpx.MockTransport`).

- [ ] **Step 2: Add a failing test for `get_akas`**

Append to `tests/unit/tvmaze/test_client.py` (adapt the mock-style to whatever the existing tests use; the snippet below assumes `respx`):

```python
import respx
import httpx
import pytest

from tvbf.tvmaze.client import TVMazeClient


@pytest.mark.asyncio
@respx.mock
async def test_get_akas_returns_list_of_dicts():
    route = respx.get("https://api.tvmaze.com/shows/123/akas").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"name": "Tokyo Revengers", "country": {"code": "US", "name": "United States"}, "language": "en"},
                {"name": "東京リベンジャーズ", "country": {"code": "JP", "name": "Japan"}, "language": "ja"},
            ],
        )
    )
    async with TVMazeClient(
        base_url="https://api.tvmaze.com", rate_calls=18, rate_window=10.0
    ) as c:
        akas = await c.get_akas(123)
    assert route.called
    assert len(akas) == 2
    assert akas[0]["name"] == "Tokyo Revengers"


@pytest.mark.asyncio
@respx.mock
async def test_get_akas_empty_list_for_shows_with_no_akas():
    respx.get("https://api.tvmaze.com/shows/999/akas").mock(
        return_value=httpx.Response(200, json=[])
    )
    async with TVMazeClient(
        base_url="https://api.tvmaze.com", rate_calls=18, rate_window=10.0
    ) as c:
        akas = await c.get_akas(999)
    assert akas == []
```

If the existing tests use a different mocking library, mirror its style instead.

- [ ] **Step 3: Run the new tests, verify they fail**

```bash
task test -- tests/unit/tvmaze/test_client.py -v -k akas
```
Expected: AttributeError "TVMazeClient has no attribute 'get_akas'".

- [ ] **Step 4: Implement `get_akas`**

In `src/tvbf/tvmaze/client.py`, add after `get_show_updates` (around line 97):

```python
    async def get_akas(self, show_id: int) -> list[dict]:
        url = f"{self._base_url}/shows/{show_id}/akas"
        resp = await self._request("GET", url)
        return resp.json()
```

The retry/rate-limit/429 handling is provided by `_request`.

- [ ] **Step 5: Run the tests, verify they pass**

```bash
task test -- tests/unit/tvmaze/test_client.py -v
```
Expected: all client tests pass (existing + 2 new).

---

## Task 5: Upsert — `upsert_akas` and a `mark_akas_synced` helper

**Files:**
- Create: `tests/integration/tvmaze/test_show_aka_upsert.py`
- Modify: `src/tvbf/tvmaze/upsert.py`

- [ ] **Step 1: Write failing integration tests**

Create `tests/integration/tvmaze/test_show_aka_upsert.py`:

```python
from datetime import UTC, datetime
import pytest
from sqlalchemy import select

from tvbf.tvmaze import models as m
from tvbf.tvmaze.schemas import TVMazeAka
from tvbf.tvmaze.upsert import mark_akas_synced, upsert_akas


@pytest.fixture
async def show_in_db(session):
    s = m.Show(
        id=4242,
        name="東京リベンジャーズ",
        tvmaze_updated=1700000000,
    )
    session.add(s)
    await session.commit()
    return s


async def test_upsert_akas_inserts_rows(session, show_in_db):
    akas = [
        TVMazeAka.model_validate(
            {"name": "Tokyo Revengers", "country": {"code": "US", "name": "United States"}, "language": "en"}
        ),
        TVMazeAka.model_validate(
            {"name": "Tokyo卍Revengers", "country": None, "language": None}
        ),
    ]
    await upsert_akas(session, show_id=4242, akas=akas)
    await session.commit()

    rows = (
        await session.execute(select(m.ShowAka).where(m.ShowAka.show_id == 4242))
    ).scalars().all()
    assert len(rows) == 2
    by_name = {r.name: r for r in rows}
    assert by_name["Tokyo Revengers"].country_code == "US"
    assert by_name["Tokyo Revengers"].language == "en"
    assert by_name["Tokyo卍Revengers"].country_code is None


async def test_upsert_akas_replaces_existing_rows(session, show_in_db):
    first = [
        TVMazeAka.model_validate({"name": "Old Title", "country": None}),
    ]
    await upsert_akas(session, show_id=4242, akas=first)
    await session.commit()

    second = [
        TVMazeAka.model_validate({"name": "New Title", "country": None}),
        TVMazeAka.model_validate({"name": "Another", "country": None}),
    ]
    await upsert_akas(session, show_id=4242, akas=second)
    await session.commit()

    rows = (
        await session.execute(
            select(m.ShowAka.name).where(m.ShowAka.show_id == 4242)
        )
    ).scalars().all()
    assert sorted(rows) == ["Another", "New Title"]


async def test_upsert_empty_clears_rows(session, show_in_db):
    await upsert_akas(
        session, show_id=4242, akas=[TVMazeAka.model_validate({"name": "X", "country": None})]
    )
    await session.commit()

    await upsert_akas(session, show_id=4242, akas=[])
    await session.commit()

    rows = (
        await session.execute(select(m.ShowAka).where(m.ShowAka.show_id == 4242))
    ).scalars().all()
    assert rows == []


async def test_mark_akas_synced_sets_timestamp(session, show_in_db):
    before = datetime.now(UTC)
    await mark_akas_synced(session, show_id=4242)
    await session.commit()

    refreshed = (
        await session.execute(
            select(m.Show).where(m.Show.id == 4242).execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert refreshed.akas_synced_at is not None
    assert refreshed.akas_synced_at >= before
```

(`session` is the existing async session fixture from `tests/conftest.py`.)

- [ ] **Step 2: Run, verify they fail**

```bash
task test -- tests/integration/tvmaze/test_show_aka_upsert.py -v
```
Expected: ImportError for `upsert_akas`/`mark_akas_synced`.

- [ ] **Step 3: Implement the helpers**

Open `src/tvbf/tvmaze/upsert.py`. Add new imports if needed (`update`, `datetime`, `UTC`):

```python
from datetime import UTC, datetime  # if not already present
```

Add at the bottom of the file:

```python
async def upsert_akas(
    session: AsyncSession, *, show_id: int, akas: list["TVMazeAka"]
) -> None:
    """Replace this show's AKA rows. Caller owns the transaction.

    AKA lists are short (typically <20 entries) and TVMaze can both add and
    remove entries between syncs, so a delete-then-insert is simpler and more
    correct than per-row upserts.
    """
    await session.execute(delete(m.ShowAka).where(m.ShowAka.show_id == show_id))
    if not akas:
        return
    rows = [
        {
            "show_id": show_id,
            "name": a.name,
            "country_code": a.country_code,
            "country_name": a.country_name,
            "language": a.language,
        }
        for a in akas
    ]
    await session.execute(insert(m.ShowAka).values(rows))


async def mark_akas_synced(session: AsyncSession, *, show_id: int) -> None:
    await session.execute(
        update(m.Show)
        .where(m.Show.id == show_id)
        .values(akas_synced_at=datetime.now(UTC))
    )
```

Add the imports `update` (from `sqlalchemy`) and the forward `TVMazeAka` import:

```python
from sqlalchemy import delete, update  # extend existing import
from tvbf.tvmaze.schemas import TVMazeAka  # extend existing import
```

If those imports already include the names, leave them; otherwise update them. Convert the string-typed `"TVMazeAka"` annotation in `upsert_akas` to a regular reference now that the import exists:

```python
async def upsert_akas(
    session: AsyncSession, *, show_id: int, akas: list[TVMazeAka]
) -> None:
```

- [ ] **Step 4: Run, verify they pass**

```bash
task test -- tests/integration/tvmaze/test_show_aka_upsert.py -v
```
Expected: 4 tests pass.

- [ ] **Step 5: Lint + typecheck**

```bash
task lint
task typecheck
```
Expected: no errors.

---

## Task 6: Wire AKAs into the per-show ingest path

**Files:**
- Modify: `src/tvbf/tvmaze/ingest.py`
- Modify: `src/tvbf/tvmaze/update.py`

We add the AKA fetch + upsert to the same per-show transaction as the show payload. Failures fetching AKAs are treated like any other per-show failure (logged, counted, retry-eligible).

- [ ] **Step 1: Update `ingest.py` per-show success branch**

Open `src/tvbf/tvmaze/ingest.py`. Replace the per-show success block (lines ~94-101 in the current file):

```python
        try:
            async with _owned_session(session_factory) as s:
                show = TVMazeShow.model_validate(payload)
                await upsert_show_payload(s, show)
                await record_progress(s, run_id, processed_delta=1)
                await s.commit()
            processed += 1
            consecutive_failures = 0
```

with:

```python
        try:
            akas_payload = await client.get_akas(show_id)
        except httpx.HTTPStatusError as e:
            log.warning("skipping akas for show %d after http error: %s", show_id, e)
            failed += 1
            consecutive_failures += 1
            async with _owned_session(session_factory) as s:
                await record_progress(s, run_id, failed_delta=1)
                await s.commit()
            if consecutive_failures >= failure_threshold:
                async with _owned_session(session_factory) as s:
                    await finalize_run(
                        s,
                        run_id,
                        status="failed",
                        error=f"aborted after {consecutive_failures} consecutive failures",
                    )
                    await s.commit()
                return IngestResult(processed, failed, cursor)
            continue

        try:
            async with _owned_session(session_factory) as s:
                show = TVMazeShow.model_validate(payload)
                await upsert_show_payload(s, show)
                akas = [TVMazeAka.model_validate(a) for a in akas_payload]
                await upsert_akas(s, show_id=show.id, akas=akas)
                await mark_akas_synced(s, show_id=show.id)
                await record_progress(s, run_id, processed_delta=1)
                await s.commit()
            processed += 1
            consecutive_failures = 0
```

Add the imports at the top of `ingest.py`:

```python
from tvbf.tvmaze.schemas import TVMazeAka, TVMazeShow  # extend existing
from tvbf.tvmaze.upsert import mark_akas_synced, upsert_akas, upsert_show_payload  # extend existing
```

- [ ] **Step 2: Update `update.py` similarly**

Open `src/tvbf/tvmaze/update.py` and find the per-show success branch (it mirrors `ingest.py`). Apply the same pattern: fetch akas → upsert payload → upsert akas → mark synced. (Re-use the structure shown in Step 1; the file's surrounding loop body and finalization are unchanged.) Add the same imports.

- [ ] **Step 3: Update existing ingest/update tests**

Run the existing suite:

```bash
task test
```
Expected: most tests still pass; some `test_ingest.py` / `test_update.py` tests may fail because the test client/fakes don't implement `get_akas`. Patch those tests to:

1. Add a `get_akas` method to the fake/mock that returns `[]` by default.
2. For tests that exercise success paths, assert that `mark_akas_synced` was called or that `show_aka` rows exist (depending on test style).

Do this minimal patching only; don't rewrite the tests broadly.

- [ ] **Step 4: Add a focused integration test for ingest-with-akas**

Append to `tests/integration/tvmaze/test_ingest.py` (or wherever the ingest happy-path test lives):

```python
async def test_ingest_writes_show_akas(session, ...):
    """When the show payload has AKAs, ingest persists them and stamps akas_synced_at."""
    # Configure the fake client so get_show returns a payload AND get_akas returns 2 entries
    # for the same show_id. Reuse the existing happy-path harness; just extend it.
    ...
    # After ingest completes:
    rows = (await session.execute(select(m.ShowAka))).scalars().all()
    assert len(rows) == 2
    show = (await session.execute(select(m.Show).where(m.Show.id == <id>))).scalar_one()
    assert show.akas_synced_at is not None
```

Pattern to follow exactly mirrors the existing happy-path test in the same file (read it first; replace `...` with the same fixtures and assertions style).

- [ ] **Step 5: Run lint, typecheck, full test suite**

```bash
task lint
task typecheck
task test
```
Expected: all green.

---

## Task 7: Browse query — match AKAs in `list_shows`

**Files:**
- Create: `tests/integration/test_browse_aka_search.py`
- Modify: `src/tvbf/tvmaze/browse_queries.py`

- [ ] **Step 1: Write failing tests**

Create `tests/integration/test_browse_aka_search.py`:

```python
import pytest
from sqlalchemy import insert

from tvbf.tvmaze import models as m
from tvbf.tvmaze.browse_queries import list_shows
from tvbf.tvmaze.dto import ShowFilters


@pytest.fixture
async def seeded_shows(session):
    """One Japanese show with English AKA, one purely English-titled show, one foreign with no AKAs."""
    rows = [
        m.Show(id=1, name="東京リベンジャーズ", tvmaze_updated=1),
        m.Show(id=2, name="Severance", tvmaze_updated=1),
        m.Show(id=3, name="進撃の巨人", tvmaze_updated=1),
    ]
    for r in rows:
        session.add(r)
    await session.flush()  # see CLAUDE.md: required between cross-table inserts
    await session.execute(
        insert(m.ShowAka).values(
            [
                {"show_id": 1, "name": "Tokyo Revengers", "country_code": "US",
                 "country_name": "United States", "language": "en"},
                {"show_id": 1, "name": "Tokyo Revengers", "country_code": "GB",
                 "country_name": "United Kingdom", "language": "en"},
            ]
        )
    )
    await session.commit()


async def test_search_matches_show_name(session, seeded_shows):
    shows, total = await list_shows(
        session, ShowFilters(search="severance"), sort="name", page=1, per_page=20
    )
    assert total == 1
    assert {s.id for s in shows} == {2}


async def test_search_matches_aka_name(session, seeded_shows):
    shows, total = await list_shows(
        session,
        ShowFilters(search="tokyo revengers"),
        sort="name",
        page=1,
        per_page=20,
    )
    assert total == 1
    assert {s.id for s in shows} == {1}


async def test_search_dedupes_when_show_has_multiple_aka_matches(session, seeded_shows):
    shows, total = await list_shows(
        session, ShowFilters(search="tokyo"), sort="name", page=1, per_page=20
    )
    assert total == 1  # not 2 (one row per matched AKA)
    assert {s.id for s in shows} == {1}


async def test_search_returns_only_shows_matching_native_when_no_aka(session, seeded_shows):
    shows, total = await list_shows(
        session, ShowFilters(search="進撃"), sort="name", page=1, per_page=20
    )
    assert total == 1
    assert {s.id for s in shows} == {3}


async def test_search_returns_empty_for_unrelated_terms(session, seeded_shows):
    shows, total = await list_shows(
        session, ShowFilters(search="frieren"), sort="name", page=1, per_page=20
    )
    assert total == 0
    assert shows == []
```

- [ ] **Step 2: Run, verify they fail**

```bash
task test -- tests/integration/test_browse_aka_search.py -v
```
Expected: `test_search_matches_aka_name` fails (returns 0 results) because the query doesn't consult `show_aka`.

- [ ] **Step 3: Update `list_shows`**

Open `src/tvbf/tvmaze/browse_queries.py`. Find the search block (lines ~129-135):

```python
    if filters.search:
        for token in filters.search.split():
            base = base.where(m.Show.name.ilike(f"%{token}%"))
```

Replace with:

```python
    if filters.search:
        for token in filters.search.split():
            needle = f"%{token}%"
            aka_subq = select(m.ShowAka.show_id).where(m.ShowAka.name.ilike(needle))
            base = base.where(or_(m.Show.name.ilike(needle), m.Show.id.in_(aka_subq)))
```

Add `or_` to the existing SQLAlchemy imports at the top of the file:

```python
from sqlalchemy import func, or_, select  # extend existing
```

The count query is built from the same `base` statement; no separate change needed.

- [ ] **Step 4: Run, verify all pass**

```bash
task test -- tests/integration/test_browse_aka_search.py -v
```
Expected: all 5 tests pass.

- [ ] **Step 5: Run the full suite**

```bash
task test
```
Expected: no regressions.

---

## Task 8: Backfill orchestrator — `tvmaze/akas_backfill.py`

**Files:**
- Create: `src/tvbf/tvmaze/akas_backfill.py`
- Create: `tests/integration/tvmaze/test_akas_backfill.py`

The backfill iterates every `tvmaze.show.id` where `akas_synced_at IS NULL`, fetches AKAs, and upserts. Resumable: a crash mid-run leaves already-synced shows alone next time. Reports progress via the same `ingest_run` row pattern.

- [ ] **Step 1: Write failing tests**

Create `tests/integration/tvmaze/test_akas_backfill.py`:

```python
from uuid import uuid4
import pytest
from sqlalchemy import select

from tvbf.tvmaze import models as m
from tvbf.tvmaze.akas_backfill import run_akas_backfill


class FakeClient:
    def __init__(self, payloads: dict[int, list[dict]]):
        self._payloads = payloads
        self.calls: list[int] = []

    async def get_akas(self, show_id: int) -> list[dict]:
        self.calls.append(show_id)
        return self._payloads.get(show_id, [])


@pytest.fixture
async def two_unsynced_shows(session):
    session.add_all(
        [
            m.Show(id=10, name="Foo", tvmaze_updated=1),
            m.Show(id=11, name="Bar", tvmaze_updated=1),
        ]
    )
    await session.commit()


async def test_backfill_processes_unsynced_shows(session, session_factory, two_unsynced_shows):
    run = m.IngestRun(id=uuid4(), kind="akas_backfill", status="running")
    session.add(run)
    await session.commit()
    client = FakeClient(
        {
            10: [{"name": "Foo (US)", "country": {"code": "US", "name": "United States"}, "language": "en"}],
            11: [],
        }
    )
    result = await run_akas_backfill(
        session_factory=session_factory, client=client, run_id=run.id
    )
    assert sorted(client.calls) == [10, 11]
    assert result.shows_processed == 2
    assert result.shows_failed == 0

    rows = (await session.execute(select(m.Show).where(m.Show.id.in_([10, 11])))).scalars().all()
    assert all(s.akas_synced_at is not None for s in rows)
    aka_rows = (await session.execute(select(m.ShowAka))).scalars().all()
    assert {r.show_id for r in aka_rows} == {10}


async def test_backfill_skips_already_synced(session, session_factory):
    from datetime import UTC, datetime
    session.add_all(
        [
            m.Show(id=20, name="A", tvmaze_updated=1, akas_synced_at=datetime.now(UTC)),
            m.Show(id=21, name="B", tvmaze_updated=1),
        ]
    )
    run = m.IngestRun(id=uuid4(), kind="akas_backfill", status="running")
    session.add(run)
    await session.commit()
    client = FakeClient({20: [{"name": "X", "country": None}], 21: []})
    await run_akas_backfill(session_factory=session_factory, client=client, run_id=run.id)
    assert client.calls == [21]


async def test_backfill_handles_per_show_failure(session, session_factory):
    import httpx
    session.add_all([m.Show(id=30, name="A", tvmaze_updated=1)])
    run = m.IngestRun(id=uuid4(), kind="akas_backfill", status="running")
    session.add(run)
    await session.commit()

    class FailingClient:
        async def get_akas(self, show_id):
            raise httpx.HTTPStatusError(
                "boom", request=httpx.Request("GET", "x"),
                response=httpx.Response(500),
            )

    result = await run_akas_backfill(
        session_factory=session_factory, client=FailingClient(), run_id=run.id
    )
    assert result.shows_failed == 1
    assert result.shows_processed == 0
    refreshed = (await session.execute(select(m.IngestRun).where(m.IngestRun.id == run.id))).scalar_one()
    assert refreshed.status in ("running", "failed")  # depending on threshold, this run may continue
```

(`session_factory` is the existing factory fixture used by `test_ingest.py`.)

- [ ] **Step 2: Run, verify they fail**

```bash
task test -- tests/integration/tvmaze/test_akas_backfill.py -v
```
Expected: ImportError for `tvbf.tvmaze.akas_backfill`.

- [ ] **Step 3: Implement the orchestrator**

Create `src/tvbf/tvmaze/akas_backfill.py`:

```python
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.tvmaze import models as m
from tvbf.tvmaze.runs import finalize_run, record_progress
from tvbf.tvmaze.schemas import TVMazeAka
from tvbf.tvmaze.upsert import mark_akas_synced, upsert_akas

log = logging.getLogger(__name__)

SessionFactory = Callable[[], AsyncSession]


@dataclass
class BackfillResult:
    shows_processed: int
    shows_failed: int


@asynccontextmanager
async def _owned_session(session_factory: SessionFactory) -> AsyncIterator[AsyncSession]:
    async with session_factory() as s:
        yield s


async def run_akas_backfill(
    *,
    session_factory: SessionFactory,
    client,  # duck-typed: needs `async get_akas(show_id) -> list[dict]`
    run_id: UUID,
    failure_threshold: int = 10,
) -> BackfillResult:
    """Iterate every show with akas_synced_at IS NULL; fetch + upsert AKAs.

    Each show runs in its own transaction so a crash mid-run leaves earlier
    shows synced. Per-show failures (HTTP/parse errors) bump shows_failed and
    abort the run after `failure_threshold` consecutive failures, mirroring
    the initial ingest pattern.
    """
    async with _owned_session(session_factory) as s:
        todo = (
            (
                await s.execute(
                    select(m.Show.id).where(m.Show.akas_synced_at.is_(None)).order_by(m.Show.id)
                )
            )
            .scalars()
            .all()
        )

    processed = 0
    failed = 0
    consecutive_failures = 0

    for show_id in todo:
        try:
            payload = await client.get_akas(show_id)
        except httpx.HTTPStatusError as e:
            log.warning("akas backfill: skipping show %d after http error: %s", show_id, e)
            failed += 1
            consecutive_failures += 1
            async with _owned_session(session_factory) as s:
                await record_progress(s, run_id, failed_delta=1)
                await s.commit()
            if consecutive_failures >= failure_threshold:
                async with _owned_session(session_factory) as s:
                    await finalize_run(
                        s,
                        run_id,
                        status="failed",
                        error=f"aborted after {consecutive_failures} consecutive failures",
                    )
                    await s.commit()
                return BackfillResult(processed, failed)
            continue
        except Exception as e:
            log.exception("akas backfill: unexpected error for show %d", show_id)
            failed += 1
            consecutive_failures += 1
            async with _owned_session(session_factory) as s:
                await record_progress(s, run_id, failed_delta=1)
                await s.commit()
            if consecutive_failures >= failure_threshold:
                async with _owned_session(session_factory) as s:
                    await finalize_run(
                        s,
                        run_id,
                        status="failed",
                        error=f"aborted after {consecutive_failures} consecutive failures: {e}",
                    )
                    await s.commit()
                return BackfillResult(processed, failed)
            continue

        try:
            async with _owned_session(session_factory) as s:
                akas = [TVMazeAka.model_validate(a) for a in payload]
                await upsert_akas(s, show_id=show_id, akas=akas)
                await mark_akas_synced(s, show_id=show_id)
                await record_progress(s, run_id, processed_delta=1)
                await s.commit()
            processed += 1
            consecutive_failures = 0
        except Exception as e:
            log.exception("akas backfill: upsert failed for show %d", show_id)
            failed += 1
            consecutive_failures += 1
            async with _owned_session(session_factory) as s:
                await record_progress(s, run_id, failed_delta=1)
                await s.commit()
            if consecutive_failures >= failure_threshold:
                async with _owned_session(session_factory) as s:
                    await finalize_run(
                        s,
                        run_id,
                        status="failed",
                        error=f"aborted after {consecutive_failures} consecutive failures: {e}",
                    )
                    await s.commit()
                return BackfillResult(processed, failed)

    async with _owned_session(session_factory) as s:
        await finalize_run(s, run_id, status="succeeded")
        await s.commit()

    return BackfillResult(processed, failed)
```

(Pattern intentionally mirrors `tvmaze/ingest.py:run_initial_ingest` — same per-show-transaction model, same failure-threshold abort semantics, same use of `runs.record_progress`/`finalize_run`.)

- [ ] **Step 4: Run, verify they pass**

```bash
task test -- tests/integration/tvmaze/test_akas_backfill.py -v
```
Expected: 3 tests pass.

- [ ] **Step 5: Lint/typecheck**

```bash
task lint
task typecheck
```
Expected: clean.

---

## Task 9: Admin endpoints — `POST /admin/backfill-akas` and `GET /admin/backfill-akas/{run_id}`

**Files:**
- Create: `tests/integration/test_admin_backfill_akas.py`
- Modify: `src/tvbf/routers/admin.py`

- [ ] **Step 1: Read the existing `/admin/ingest` route**

Open `src/tvbf/routers/admin.py` and study how `POST /admin/ingest` is wired:
- Bearer-token auth via the existing `require_admin` dependency
- Returns `202 Accepted` with `{"run_id": str(uuid)}`
- Spawns the orchestrator via `asyncio.create_task`
- The `GET /admin/ingest/{run_id}` reads the row and returns its current state

We mirror that exactly.

- [ ] **Step 2: Write failing tests**

Create `tests/integration/test_admin_backfill_akas.py`:

```python
import uuid
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from tvbf.main import app
from tvbf.tvmaze import models as m


@pytest.fixture
def admin_token(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "secret")
    return "secret"


async def test_backfill_post_returns_202_and_creates_run(session, admin_token):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post(
            "/admin/backfill-akas", headers={"Authorization": f"Bearer {admin_token}"}
        )
    assert resp.status_code == 202
    body = resp.json()
    assert "run_id" in body
    run_id = uuid.UUID(body["run_id"])

    row = (await session.execute(select(m.IngestRun).where(m.IngestRun.id == run_id))).scalar_one()
    assert row.kind == "akas_backfill"
    assert row.status in ("running", "succeeded", "failed")  # may finish quickly with no shows


async def test_backfill_status_returns_404_for_missing(admin_token):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.get(
            f"/admin/backfill-akas/{uuid.uuid4()}",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
    assert resp.status_code == 404


async def test_backfill_post_requires_admin_token():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post("/admin/backfill-akas")
    assert resp.status_code in (401, 403)
```

- [ ] **Step 3: Run, verify they fail**

```bash
task test -- tests/integration/test_admin_backfill_akas.py -v
```
Expected: 404 because the routes don't exist yet.

- [ ] **Step 4: Add the routes**

Open `src/tvbf/routers/admin.py`. Add new imports at the top:

```python
from tvbf.tvmaze.akas_backfill import run_akas_backfill
```

After the existing `/admin/ingest/{run_id}` route, add:

```python
@router.post("/admin/backfill-akas", status_code=202)
async def post_backfill_akas(
    bg: BackgroundTasks,
    _: None = Depends(require_admin),
    session_factory: SessionFactory = Depends(get_session_factory),
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    run_id = uuid4()
    async with session_factory() as s:
        s.add(m.IngestRun(id=run_id, kind="akas_backfill", status="running"))
        await s.commit()

    async def _run():
        client = TVMazeClient(
            base_url=settings.tvmaze_base_url,
            rate_calls=settings.tvmaze_rate_calls,
            rate_window=settings.tvmaze_rate_window,
        )
        async with client:
            await run_akas_backfill(
                session_factory=session_factory, client=client, run_id=run_id
            )

    asyncio.create_task(_run())
    return {"run_id": str(run_id)}


@router.get("/admin/backfill-akas/{run_id}")
async def get_backfill_akas(
    run_id: UUID,
    _: None = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = (
        await session.execute(select(m.IngestRun).where(m.IngestRun.id == run_id))
    ).scalar_one_or_none()
    if row is None or row.kind != "akas_backfill":
        raise HTTPException(status_code=404)
    return {
        "run_id": str(row.id),
        "kind": row.kind,
        "status": row.status,
        "started_at": row.started_at.isoformat(),
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "shows_processed": row.shows_processed,
        "shows_failed": row.shows_failed,
        "error": row.error,
    }
```

If the existing `/admin/ingest` uses helpers (e.g., a function that builds the same response dict), reuse them rather than duplicating the response shape. Read the existing file first and DRY where possible — the goal is parallelism with `/admin/ingest`, not a copy.

- [ ] **Step 5: Run, verify they pass**

```bash
task test -- tests/integration/test_admin_backfill_akas.py -v
```
Expected: 3 tests pass.

- [ ] **Step 6: Run the full suite**

```bash
task test
task lint
task typecheck
```
Expected: clean.

---

## Task 10: Stale-run cleanup — include `'akas_backfill'`

**Files:**
- Modify: `src/tvbf/main.py`

The startup lifespan hook marks runs whose `last_progress_at` is older than `INGEST_STALE_RUN_MINUTES` as `cancelled`. It currently filters by `kind IN ('initial', 'update')`. We extend it to include `'akas_backfill'` so a crashed backfill is correctly marked cancelled and a new run can start.

- [ ] **Step 1: Locate the stale-run cleanup code in `main.py`**

Open `src/tvbf/main.py`. Search for `last_progress_at` or `cancelled`. The cleanup likely uses an `IN` clause over `kind`.

- [ ] **Step 2: Update the kind list**

Change the in-clause from `('initial', 'update')` to `('initial', 'update', 'akas_backfill')`. If the cleanup uses a constant or helper, update it there.

- [ ] **Step 3: If `tests/integration/test_startup_cleanup.py` exists, add a case**

Add a test that creates a stale `kind='akas_backfill'` run and asserts it's cancelled by the lifespan. Mirror an existing test in that file.

- [ ] **Step 4: Run the test suite**

```bash
task test
task lint
task typecheck
```
Expected: clean.

---

## Task 11: Taskfile + admin.py docs — `task akas:backfill` / `:status`

**Files:**
- Modify: `tvbf-backend/Taskfile.yml`

- [ ] **Step 1: Add backfill targets to Taskfile**

Open `tvbf-backend/Taskfile.yml`. After the existing `ingest` / `ingest:status` tasks, add:

```yaml
  akas:backfill:
    desc: Trigger a backfill of TVMaze AKAs for every show in the catalog.
    cmds:
      - |
        curl -sf -X POST \
          -H "Authorization: Bearer $ADMIN_TOKEN" \
          https://tvbf-backend.localhost/admin/backfill-akas

  akas:backfill:status:
    desc: 'Poll an AKA backfill run. Usage: task akas:backfill:status -- <run_id>'
    cmds:
      - |
        curl -sf \
          -H "Authorization: Bearer $ADMIN_TOKEN" \
          https://tvbf-backend.localhost/admin/backfill-akas/{{.CLI_ARGS}}
```

(Match the exact format of the existing `ingest` task — read it first; my snippet above may differ in flag style.)

- [ ] **Step 2: Smoke test**

With the container running and one or two shows in `tvbf` whose `akas_synced_at IS NULL`:

```bash
task akas:backfill
```
Expected: `202` and a JSON `{"run_id": "..."}`.

```bash
task akas:backfill:status -- <run_id>
```
Expected: shape matches what we return in the GET handler.

- [ ] **Step 3: Verify rows and timestamps**

```bash
docker exec tbc_postgresql_db psql -U root -d tvbf -tAc \
  "SELECT COUNT(*) FROM tvmaze.show_aka"
docker exec tbc_postgresql_db psql -U root -d tvbf -tAc \
  "SELECT COUNT(*) FROM tvmaze.show WHERE akas_synced_at IS NOT NULL"
```
Expected: counts grow as the backfill processes shows.

---

## Task 12: End-to-end smoke + final gate

**Files:** none (verification only).

- [ ] **Step 1: Run the full quality gate**

```bash
task lint
task typecheck
task test
task coverage
```
Expected: green; coverage on the new modules ≥ existing repo norms.

- [ ] **Step 2: Manual end-to-end check via `/docs`**

1. With the dev stack running, browse to `https://tvbf-backend.localhost/docs`.
2. Pick a show in the catalog whose primary name is non-Latin (e.g., `東京リベンジャーズ` if present, or any Japanese/Korean show — `SELECT id, name FROM tvmaze.show WHERE name !~ '^[\x00-\x7f]*$' LIMIT 5` finds candidates).
3. Run `task akas:backfill`, wait for it to process at least that show, confirm `show_aka` rows exist.
4. Hit `GET /shows?search=<english title>` and verify the show appears.
5. Hit `GET /shows?search=<native title fragment>` and verify the same show still appears (regression check).

- [ ] **Step 3: Spot-check pagination + sort**

```bash
curl -s "https://tvbf-backend.localhost/shows?search=tokyo&sort=name&page=1&per_page=20" | jq '.total, .items | length'
```
Expected: `total` reflects deduped show count; `items` length ≤ `per_page`. Sort still operates on `show.name`, not AKA names (per spec).

---

## Self-review notes

- Spec coverage: Tasks 1-2 cover the schema; Task 3 the Pydantic shape; Task 4 the client; Tasks 5-6 the upsert + ingest + update wiring; Task 7 the search behavior; Tasks 8-11 the backfill (orchestrator, admin endpoints, stale cleanup, Taskfile); Task 12 verification.
- The deferred decisions (language filter at search time; surfacing matched AKA in responses; the future English-as-primary-title display) deliberately stay out of this plan — they're future work per the spec.
- `or_` is added to the SQLAlchemy import in Task 7. `update` is added in Task 5. `delete` is already imported in `upsert.py` — confirm before editing.
- Task 6 step 3 is intentionally vague about which existing tests need patching because that depends on the current shape of the test fakes. The key invariant to preserve: the new code path calls `client.get_akas(show_id)` per show; any fake that doesn't implement it must grow that method (returning `[]` is the sensible default for tests not exercising AKAs).
