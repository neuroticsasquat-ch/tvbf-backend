# Cast and Crew Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Per repo convention, this plan contains NO `git commit` steps — the user commits at their own cadence.

**Goal:** Mirror TV Maze's people data as a second ingest axis of the catalog, and surface it: cast and crew on show pages, guest cast on episode pages, person pages with complete filmographies, and person search.

**Architecture:** Six new `tvmaze` tables (`person`, `character`, `crew_role`, `show_cast`, `show_crew`, `episode_guest_cast`). Two backfill passes sharing one **process-wide** rate limiter — **pass A** (~27h, show refresh: cast, crew, `externals_tvdb`, ratings, specials) and **pass C** (~75h, person ingest + guest cast). Concurrent jobs split a single 18 req/10s budget and each simply run slower. This was only made true by NEU-955: before it, `TVMazeClient.__init__` built its own `RateLimiter` per instance and every admin route built its own client, so two concurrent jobs each paced at the full rate and hit TV Maze at ~36 req/10s. A deployment predating NEU-955 still behaves that way — suspend the daily update for the duration of a long pass there (Coolify → tvbf-backend → Scheduled Tasks → "daily-update"; it was a GitHub Actions workflow until NEU-1008). `person` becomes a peer mirrored entity with its own initial ingest and daily delta driven by `/updates/people`. Credit rows use delete-then-insert with no unique constraint, mirroring `upsert_akas`. New read endpoints on the browse router; new `/people/:id` route and a multi-entity search overlay on the frontend.

**Tech Stack:** FastAPI, async SQLAlchemy, asyncpg, Alembic, Pydantic v2, pytest with ASGITransport. React 19 + Vite 6 + TypeScript + Tailwind v4 + shadcn/ui + React Query + MSW + Vitest. All commands run inside the `tvbf_backend` / `tvbf_frontend` containers via `task` targets.

**Spec:** `docs/superpowers/specs/2026-08-01-cast-and-crew-design.md`
**ADRs:** `docs/adr/0001-tvmaze-second-ingest-axis.md`, `docs/adr/0002-no-upstream-api-in-request-path.md`
**Glossary:** `CONTEXT.md`

> **Base branch: `release/v0.2.0`, not `main`.** Every ticket in this plan branches from and targets `release/v0.2.0`:
> `git checkout -b <branch> origin/release/v0.2.0` … `gh pr create --base release/v0.2.0`.
> Don't infer this from `gh pr list` — recent merged PRs targeted `main` back when the two branches were identical.

---

## Corrections discovered while reading the code

Four things differ from what CLAUDE.md or the spec says. **Trust this list over both.**

1. **Migrations live in `tvbf-backend/migrations/`, not `alembic/`.** CLAUDE.md says `alembic/`. Current head is **`c2e451aa1ec6`** (`add_immutable_unaccent_and_folded_trgm_`). Migration files use bare `revision = '...'` assignments, not typed `revision: str = ...`.

2. **Browse cache is `private, max-age=300`, not `public`.** `routers/browse.py` defines `_set_browse_cache` as a router-level dependency with `private` — deliberately, because browse is session-gated and shared caches must not fan a response across users. Separately, `/shows` and `/shows/{id}` use `private, no-store` because they carry per-user `my_rating`. **Cast/crew/person routes carry no per-user fields, so they take the router default (`private, max-age=300`) and need no override.**

3. **`get_last_successful_cursor` was not axis-aware** (`runs.py:49`) — **already fixed and merged as NEU-954**, ahead of this plan. It selected the most recent succeeded run with a non-null cursor regardless of `kind`, so two deltas sharing that column would read each other's watermark and silently skip work. Scoping is **per axis, not per kind** (`SHOW_CURSOR_KINDS` / `PERSON_CURSOR_KINDS`) because the initial ingest hands its cursor to the first delta. See Task 6.

4. **`/shows/{id}/episodes?specials=1` returns *all* episodes, not just specials** (92 vs. the embed's 91 for Chuck). So pass A does **not** need `embed[]=episodes` at all — dropping it shrinks the payload and removes a merge step. The embed is still needed for `seasons`, `cast`, `crew`.

Also worth knowing: **`update.py` already makes a second request per show** (`get_akas`, line 56). The daily delta is 2 req/show today and becomes 3 with specials.

---

## Prerequisites (not tasks in this plan)

- **NEU-922** — the `thetvdb` alias fix in `api_payloads.py`. Must be merged and deployed before pass A runs, or 27 unrepeatable hours write `NULL`.
- Tests build their schema from `Base.metadata.create_all` (see `tests/conftest.py`), **not** from migrations. Model correctness is what the suite exercises; migration correctness needs `task migrate` against a real DB.

---

## File map

### Backend — create

- `migrations/versions/<rev>_add_credits_tables.py` — six tables, two watermark columns, `ingest_run.kind` widening.
- `migrations/versions/<rev>_add_person_name_trgm.py` — folded trigram index on `person.name`.
- `src/tvbf/tvmaze/show_refresh.py` — pass A orchestrator.
- `src/tvbf/tvmaze/person_ingest.py` — pass C orchestrator.
- `src/tvbf/tvmaze/person_update.py` — daily person delta.
- `tests/unit/tvmaze/test_credit_payloads.py`
- `tests/unit/tvmaze/test_person_payloads.py`
- `tests/integration/tvmaze/test_credit_upsert.py`
- `tests/integration/tvmaze/test_show_refresh.py`
- `tests/integration/tvmaze/test_person_ingest.py`
- `tests/integration/tvmaze/test_person_update.py`
- `tests/integration/routers/test_credits_routes.py`
- `tests/integration/routers/test_people_routes.py`
- `tests/fixtures/tvmaze/credit_factory.py`

### Backend — modify

- `src/tvbf/tvmaze/models.py` — six models; `Show.credits_synced_at`; `IngestRun.kind` constraint + length.
- `src/tvbf/tvmaze/api_payloads.py` — `TVMazePerson`, `TVMazeCharacter`, `TVMazeCastEntry`, `TVMazeCrewEntry`, `TVMazeGuestCastCredit`; `cast`/`crew` on `TVMazeEmbedded`.
- `src/tvbf/tvmaze/client.py` — honour `embed`; add `get_show_episodes`, `get_person`, `get_person_updates`.
- `src/tvbf/tvmaze/upsert.py` — person/character/crew-role upserts, credit delete-then-insert, watermark stamps.
- `src/tvbf/tvmaze/runs.py` — make `get_last_successful_cursor` kind-aware.
- `src/tvbf/tvmaze/ingest.py`, `src/tvbf/tvmaze/update.py` — specials + cast/crew on the ongoing path (NEU-933).
- `src/tvbf/tvmaze/schemas.py` — credit + person response models.
- `src/tvbf/tvmaze/browse_queries.py` — credit and person queries; export `_fold` for reuse.
- `src/tvbf/routers/browse.py` — six new routes.
- `src/tvbf/routers/admin.py` — three new trigger/status pairs.
- `Taskfile.yml` — `refresh:shows`, `people:ingest`, `people:update` (+ status).

### Frontend — create

- `src/api/people.ts`, `src/pages/PersonPage.tsx`, `src/components/CastList.tsx`, `CrewList.tsx`, `PersonChip.tsx`

### Frontend — modify

- `src/api/types.ts`, `src/api/shows.ts`, `src/router.tsx`, `src/pages/ShowDetailPage.tsx`, `src/pages/EpisodePage.tsx`, `src/components/SearchOverlay.tsx`, `src/test/msw/handlers.ts`

---

# Milestone 1 — Show-level credits

## Task 1: Models — six credit tables + watermark + run kinds

**Ticket:** NEU-936

- [ ] Add to `src/tvbf/tvmaze/models.py`:

```python
class Person(Base):
    __tablename__ = "person"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    country_code: Mapped[str | None] = mapped_column(Text)
    country_name: Mapped[str | None] = mapped_column(Text)
    timezone: Mapped[str | None] = mapped_column(Text)
    birthday: Mapped[date | None] = mapped_column(Date)
    deathday: Mapped[date | None] = mapped_column(Date)
    gender: Mapped[str | None] = mapped_column(Text)
    image_medium: Mapped[str | None] = mapped_column(Text)
    image_original: Mapped[str | None] = mapped_column(Text)
    tvmaze_updated: Mapped[int] = mapped_column(BigInteger, nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # Pass C watermark. Set only when this person's credits have been fetched —
    # a person row created from a show's cast embed has credits_synced_at NULL.
    credits_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Character(Base):
    __tablename__ = "character"
    __table_args__ = {"schema": SCHEMA}
    # No show_id: upstream provides none (/characters/{id} has no show link),
    # and the character->show relationship is carried by the credit rows.
    # A character is not owned by one person — The Simpsons credits both
    # Hank Azaria and Harry Shearer as Carl Carlson.

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    image_medium: Mapped[str | None] = mapped_column(Text)
    image_original: Mapped[str | None] = mapped_column(Text)


class CrewRole(Base):
    __tablename__ = "crew_role"
    __table_args__ = (
        UniqueConstraint("name", name="uq_crew_role_name"),
        {"schema": SCHEMA},
    )
    # Upstream sends crew type as a bare string with no id, exactly like genre.
    # Modeled on Genre: local autoincrement id, unique name.

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)


class ShowCast(Base):
    __tablename__ = "show_cast"
    __table_args__ = (
        Index("ix_show_cast_show_id_sort", "show_id", "sort_order"),
        Index("ix_show_cast_person_id", "person_id"),
        {"schema": SCHEMA},
    )
    # No UNIQUE(show_id, person_id, character_id) — deliberate. Refresh is
    # delete-then-insert (see upsert_show_cast), so there is nothing to conflict
    # on, and a uniqueness assumption over upstream data is what broke ingestion
    # on tvmaze.season. sort_order preserves upstream billing order.

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    show_id: Mapped[int] = mapped_column(
        ForeignKey(f"{SCHEMA}.show.id", ondelete="CASCADE"), nullable=False
    )
    person_id: Mapped[int] = mapped_column(ForeignKey(f"{SCHEMA}.person.id"), nullable=False)
    character_id: Mapped[int] = mapped_column(
        ForeignKey(f"{SCHEMA}.character.id"), nullable=False
    )
    is_self: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_voice: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)


class ShowCrew(Base):
    __tablename__ = "show_crew"
    __table_args__ = (
        Index("ix_show_crew_show_id_sort", "show_id", "sort_order"),
        Index("ix_show_crew_person_id", "person_id"),
        {"schema": SCHEMA},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    show_id: Mapped[int] = mapped_column(
        ForeignKey(f"{SCHEMA}.show.id", ondelete="CASCADE"), nullable=False
    )
    person_id: Mapped[int] = mapped_column(ForeignKey(f"{SCHEMA}.person.id"), nullable=False)
    role_id: Mapped[int] = mapped_column(ForeignKey(f"{SCHEMA}.crew_role.id"), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)


class EpisodeGuestCast(Base):
    __tablename__ = "episode_guest_cast"
    __table_args__ = (
        Index("ix_egc_episode_id_sort", "episode_id", "sort_order"),
        Index("ix_egc_person_id", "person_id"),
        {"schema": SCHEMA},
    )
    # Written by the PERSON axis, not the show axis. Refresh grain is
    # WHERE person_id = ? — never per-episode. See ADR-0001.

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    episode_id: Mapped[int] = mapped_column(
        ForeignKey(f"{SCHEMA}.episode.id", ondelete="CASCADE"), nullable=False
    )
    person_id: Mapped[int] = mapped_column(ForeignKey(f"{SCHEMA}.person.id"), nullable=False)
    character_id: Mapped[int] = mapped_column(
        ForeignKey(f"{SCHEMA}.character.id"), nullable=False
    )
    is_self: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_voice: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)
```

- [ ] Add `Boolean` and `Index` to the `sqlalchemy` import block at the top of the file.
- [ ] Add to `Show`: `credits_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))`
- [ ] Widen `IngestRun.kind` and extend its check constraint:

```python
        CheckConstraint(
            "kind IN ('initial', 'update', 'akas_backfill', 'ratings_backfill', "
            "'show_refresh', 'person_initial', 'person_update')",
            name="ck_ingest_run_kind",
        ),
```
```python
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
```

**Why 32 and not 16:** the column is `String(16)` today. `'person_initial'` is 14 and fits, but the margin is one character and the next kind added will not. Widen once.

- [ ] `task typecheck` passes.

---

## Task 2: Migration — credit tables

**Ticket:** NEU-936

- [ ] `task makemigration -- "add credit tables"`, then **hand-review the autogenerated file** — autogenerate will not produce the check-constraint rewrite and may emit spurious drops.
- [ ] The migration must contain, in order:

```python
revision = '<generated>'
down_revision = 'c2e451aa1ec6'


def upgrade() -> None:
    op.create_table(
        'person',
        sa.Column('id', sa.Integer(), autoincrement=False, nullable=False),
        sa.Column('name', sa.Text(), nullable=False),
        sa.Column('country_code', sa.Text(), nullable=True),
        sa.Column('country_name', sa.Text(), nullable=True),
        sa.Column('timezone', sa.Text(), nullable=True),
        sa.Column('birthday', sa.Date(), nullable=True),
        sa.Column('deathday', sa.Date(), nullable=True),
        sa.Column('gender', sa.Text(), nullable=True),
        sa.Column('image_medium', sa.Text(), nullable=True),
        sa.Column('image_original', sa.Text(), nullable=True),
        sa.Column('tvmaze_updated', sa.BigInteger(), nullable=False),
        sa.Column('ingested_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.Column('credits_synced_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        schema='tvmaze',
    )
    op.create_table(
        'character',
        sa.Column('id', sa.Integer(), autoincrement=False, nullable=False),
        sa.Column('name', sa.Text(), nullable=False),
        sa.Column('image_medium', sa.Text(), nullable=True),
        sa.Column('image_original', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        schema='tvmaze',
    )
    op.create_table(
        'crew_role',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('name', sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name', name='uq_crew_role_name'),
        schema='tvmaze',
    )
    # show_cast / show_crew / episode_guest_cast — BigInteger identity PKs,
    # FKs as declared in models.py, then the six indexes.
    ...
    op.add_column('show',
        sa.Column('credits_synced_at', sa.DateTime(timezone=True), nullable=True),
        schema='tvmaze')

    # Widen kind and rewrite the whitelist. Drop before alter — the constraint
    # references the column being changed.
    op.drop_constraint('ck_ingest_run_kind', 'ingest_run', schema='tvmaze', type_='check')
    op.alter_column('ingest_run', 'kind', schema='tvmaze',
                    existing_type=sa.String(16), type_=sa.String(32), existing_nullable=False)
    op.create_check_constraint(
        'ck_ingest_run_kind', 'ingest_run',
        "kind IN ('initial', 'update', 'akas_backfill', 'ratings_backfill', "
        "'show_refresh', 'person_initial', 'person_update')",
        schema='tvmaze',
    )
```

- [ ] `downgrade()` reverses in strict reverse order: restore the old check constraint, narrow the column, drop `show.credits_synced_at`, drop the three credit tables, then `crew_role`, `character`, `person`.
- [ ] `task migrate` succeeds against `tvbf`.
- [ ] `docker exec tbc_postgresql_db psql -U root -d tvbf -c "\d tvmaze.show_cast"` shows the expected indexes and **no unique constraint**.

---

## Task 3: Payloads — cast, crew, person, character

**Ticket:** NEU-937

- [ ] Write the tests first, in `tests/unit/tvmaze/test_credit_payloads.py`. Use **verbatim upstream JSON**, not hand-simplified dicts — this is the NEU-922 lesson, where a field that never parsed produced no error and silently nulled 87k rows.

```python
from tvbf.tvmaze.api_payloads import TVMazeCastEntry, TVMazeCrewEntry, TVMazePerson

RAW_CAST = {
    "person": {
        "id": 30856, "url": "https://www.tvmaze.com/people/30856/zachary-levi",
        "name": "Zachary Levi",
        "country": {"name": "United States", "code": "US", "timezone": "America/New_York"},
        "birthday": "1980-09-29", "deathday": None, "gender": "Male",
        "image": {"medium": "https://static.tvmaze.com/m.jpg",
                  "original": "https://static.tvmaze.com/o.jpg"},
        "updated": 1774528332,
    },
    "character": {
        "id": 45090, "url": "https://www.tvmaze.com/characters/45090/chuck",
        "name": 'Charles "Chuck" Bartowski',
        "image": {"medium": "https://static.tvmaze.com/cm.jpg", "original": None},
    },
    "self": False,
    "voice": False,
}

RAW_CREW = {
    "type": "Co-Executive Producer",
    "person": {"id": 795, "name": "Matthew Miller", "country": None,
               "birthday": None, "deathday": None, "gender": "Male",
               "image": None, "updated": 1738338751},
}


def test_cast_entry_parses_person_character_and_flags():
    e = TVMazeCastEntry.model_validate(RAW_CAST)
    assert e.person.id == 30856
    assert e.person.country_code == "US"
    assert e.person.timezone == "America/New_York"
    assert e.character.name == 'Charles "Chuck" Bartowski'
    assert e.is_self is False and e.is_voice is False


def test_cast_self_flag_parses_from_reserved_name():
    # `self` is aliased; a naive field name silently never populates.
    e = TVMazeCastEntry.model_validate({**RAW_CAST, "self": True, "voice": True})
    assert e.is_self is True and e.is_voice is True


def test_crew_entry_parses_type_and_person():
    e = TVMazeCrewEntry.model_validate(RAW_CREW)
    assert e.type == "Co-Executive Producer"
    assert e.person.id == 795
    assert e.person.country_code is None


def test_person_empty_string_dates_become_none():
    # TV Maze sends "" not null for unknown dates. A bare `date | None` raises.
    p = TVMazePerson.model_validate(
        {"id": 1, "name": "X", "birthday": "", "deathday": "", "updated": 1}
    )
    assert p.birthday is None and p.deathday is None
```

- [ ] Then add to `api_payloads.py`:

```python
class TVMazePerson(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    name: str
    country: dict | None = None
    birthday: OptionalDate = None
    deathday: OptionalDate = None
    gender: str | None = None
    image: TVMazeImage | None = None
    updated: int = 0

    @property
    def country_code(self) -> str | None:
        return (self.country or {}).get("code")

    @property
    def country_name(self) -> str | None:
        return (self.country or {}).get("name")

    @property
    def timezone(self) -> str | None:
        return (self.country or {}).get("timezone")


class TVMazeCharacter(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    name: str
    image: TVMazeImage | None = None


class TVMazeCastEntry(BaseModel):
    # `self` can't be a field name, so both flags are aliased for symmetry.
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    person: TVMazePerson
    character: TVMazeCharacter
    is_self: bool = Field(False, alias="self")
    is_voice: bool = Field(False, alias="voice")


class TVMazeCrewEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: str
    person: TVMazePerson
```

- [ ] Extend `TVMazeEmbedded`:

```python
class TVMazeEmbedded(BaseModel):
    model_config = ConfigDict(extra="ignore")

    episodes: list[TVMazeEpisode] = Field(default_factory=list)
    seasons: list[TVMazeSeason] = Field(default_factory=list)
    cast: list[TVMazeCastEntry] = Field(default_factory=list)
    crew: list[TVMazeCrewEntry] = Field(default_factory=list)
```

- [ ] `task test -- tests/unit/tvmaze/test_credit_payloads.py` passes.

---

## Task 4: Client — honour `embed`, add specials fetch

**Tickets:** NEU-937, NEU-933

`get_show` currently does `del embed` and hardcodes `episodes` + `seasons` (`client.py:87–96`). Make it real.

- [ ] Replace `get_show` and add `get_show_episodes`:

```python
    _DEFAULT_EMBEDS = ("episodes", "seasons")

    async def get_show(self, show_id: int, *, embed: list[str] | None = None) -> dict:
        embeds = tuple(embed) if embed is not None else self._DEFAULT_EMBEDS
        url = f"{self._base_url}/shows/{show_id}"
        resp = await self._request("GET", url, params=[("embed[]", e) for e in embeds])
        return resp.json()

    async def get_show_episodes(self, show_id: int, *, specials: bool = True) -> list[dict]:
        """Full episode list for a show.

        The `embed[]=episodes` form silently omits specials and ignores a
        `specials=1` query param, so specials are only reachable here. This
        endpoint returns ALL episodes including specials, so callers using it
        do not also need embed[]=episodes.
        """
        url = f"{self._base_url}/shows/{show_id}/episodes"
        params = [("specials", "1")] if specials else []
        resp = await self._request("GET", url, params=params)
        return resp.json()
```

- [ ] Verify the existing `ratings_backfill.py` call site — it passes `embed=["episodes"]`, which was a no-op and now genuinely drops the `seasons` embed. Change it to `embed=["episodes", "seasons"]` or drop the argument so it takes the default. **Not doing this silently breaks season upserts in the ratings backfill.**
- [ ] Existing ingest/update tests still pass: `task test -- tests/integration/tvmaze/`.

---

## Task 5: Upserts — persons, characters, crew roles, credits

**Ticket:** NEU-937

- [ ] Tests first, `tests/integration/tvmaze/test_credit_upsert.py`:

```python
import pytest
from sqlalchemy import select

from tvbf.tvmaze import models as m
from tvbf.tvmaze.api_payloads import TVMazeCastEntry, TVMazeCrewEntry
from tvbf.tvmaze.upsert import upsert_show_cast, upsert_show_crew


def cast_entry(person_id: int, character_id: int, name: str = "P") -> TVMazeCastEntry:
    return TVMazeCastEntry.model_validate({
        "person": {"id": person_id, "name": name, "updated": 1},
        "character": {"id": character_id, "name": f"C{character_id}"},
        "self": False, "voice": False,
    })


@pytest.fixture
async def a_show(session):
    session.add(m.Show(id=1, name="S", tvmaze_updated=1))
    await session.commit()


async def test_cast_insert_preserves_upstream_order(session, a_show):
    entries = [cast_entry(10, 100), cast_entry(11, 101), cast_entry(12, 102)]
    await upsert_show_cast(session, show_id=1, entries=entries)
    await session.commit()

    rows = (await session.execute(
        select(m.ShowCast.person_id).where(m.ShowCast.show_id == 1)
        .order_by(m.ShowCast.sort_order)
    )).scalars().all()
    assert rows == [10, 11, 12]   # billing order, not id order


async def test_reupsert_removes_departed_member(session, a_show):
    await upsert_show_cast(session, show_id=1, entries=[cast_entry(10, 100), cast_entry(11, 101)])
    await session.commit()
    await upsert_show_cast(session, show_id=1, entries=[cast_entry(11, 101)])
    await session.commit()

    rows = (await session.execute(
        select(m.ShowCast.person_id).where(m.ShowCast.show_id == 1)
    )).scalars().all()
    assert rows == [11]


async def test_duplicate_upstream_entries_are_deduped(session, a_show):
    # Upstream sending the same credit twice is one credit, not two rows.
    await upsert_show_cast(session, show_id=1, entries=[cast_entry(10, 100), cast_entry(10, 100)])
    await session.commit()
    count = (await session.execute(
        select(m.ShowCast).where(m.ShowCast.show_id == 1)
    )).scalars().all()
    assert len(count) == 1


async def test_same_character_two_people_both_kept(session, a_show):
    # The Simpsons: Hank Azaria AND Harry Shearer both credited as Carl Carlson.
    await upsert_show_cast(session, show_id=1, entries=[cast_entry(10, 100), cast_entry(11, 100)])
    await session.commit()
    rows = (await session.execute(
        select(m.ShowCast.person_id).where(m.ShowCast.show_id == 1)
        .order_by(m.ShowCast.sort_order)
    )).scalars().all()
    assert rows == [10, 11]


async def test_crew_role_resolve_is_idempotent(session, a_show):
    e = TVMazeCrewEntry.model_validate(
        {"type": "Editor", "person": {"id": 20, "name": "E", "updated": 1}})
    await upsert_show_crew(session, show_id=1, entries=[e])
    await session.commit()
    await upsert_show_crew(session, show_id=1, entries=[e])
    await session.commit()

    roles = (await session.execute(select(m.CrewRole))).scalars().all()
    assert len(roles) == 1


async def test_large_cast_exceeds_batch_size(session, a_show):
    # The Simpsons has 1,420 cast rows. Guard the bind-parameter ceiling.
    entries = [cast_entry(1000 + i, 5000 + i) for i in range(1200)]
    await upsert_show_cast(session, show_id=1, entries=entries)
    await session.commit()
    rows = (await session.execute(
        select(m.ShowCast).where(m.ShowCast.show_id == 1))).scalars().all()
    assert len(rows) == 1200
```

- [ ] Then add to `upsert.py`:

```python
# Postgres caps bind parameters per query at 32767. show_cast binds 6 columns
# per row, episode_guest_cast 6, show_crew 4 — but persons/characters upsert in
# the same transaction, so keep the same 1000-row batch as episodes rather than
# computing a tighter bound. The Simpsons (1,420 cast) exceeds one batch.
_CREDIT_BATCH_SIZE = 1000


async def upsert_persons(session: AsyncSession, people: list[TVMazePerson]) -> None:
    """Upsert person rows by upstream id. Never touches credits_synced_at —
    a person created here still needs pass C to fetch their credits."""
    if not people:
        return
    seen: dict[int, TVMazePerson] = {}
    for p in people:
        seen[p.id] = p          # last write wins within one payload
    rows = [
        {
            "id": p.id,
            "name": p.name,
            "country_code": p.country_code,
            "country_name": p.country_name,
            "timezone": p.timezone,
            "birthday": p.birthday,
            "deathday": p.deathday,
            "gender": p.gender,
            "image_medium": p.image.medium if p.image else None,
            "image_original": p.image.original if p.image else None,
            "tvmaze_updated": p.updated,
        }
        for p in seen.values()
    ]
    for start in range(0, len(rows), _CREDIT_BATCH_SIZE):
        chunk = rows[start : start + _CREDIT_BATCH_SIZE]
        stmt = insert(m.Person).values(chunk)
        stmt = stmt.on_conflict_do_update(
            index_elements=[m.Person.id],
            set_={c: getattr(stmt.excluded, c) for c in chunk[0] if c != "id"},
        )
        await session.execute(stmt)


async def upsert_characters(session: AsyncSession, characters: list[TVMazeCharacter]) -> None:
    if not characters:
        return
    seen: dict[int, TVMazeCharacter] = {c.id: c for c in characters}
    rows = [
        {
            "id": c.id,
            "name": c.name,
            "image_medium": c.image.medium if c.image else None,
            "image_original": c.image.original if c.image else None,
        }
        for c in seen.values()
    ]
    for start in range(0, len(rows), _CREDIT_BATCH_SIZE):
        chunk = rows[start : start + _CREDIT_BATCH_SIZE]
        stmt = insert(m.Character).values(chunk)
        stmt = stmt.on_conflict_do_update(
            index_elements=[m.Character.id],
            set_={c: getattr(stmt.excluded, c) for c in chunk[0] if c != "id"},
        )
        await session.execute(stmt)


async def resolve_crew_role(session: AsyncSession, name: str) -> int:
    """Resolve-or-insert a crew role id. Mirrors upsert_genre_by_name.

    NOT cached across shows, despite the ticket and spec §crew_role calling for
    a run-lifetime name->id cache. Credits are written in per-show transactions
    and a per-show failure rolls back without aborting the run, so a cached id
    for a role inserted inside a transaction that later rolled back would give
    every later show using that role an FK violation on show_crew.role_id.
    Recorded here so it doesn't get re-added (shipped uncached in NEU-937).
    """
    existing = (
        await session.execute(select(m.CrewRole.id).where(m.CrewRole.name == name))
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    stmt = (
        insert(m.CrewRole)
        .values(name=name)
        .on_conflict_do_nothing(index_elements=[m.CrewRole.name])
        .returning(m.CrewRole.id)
    )
    result = (await session.execute(stmt)).scalar_one_or_none()
    if result is not None:
        return result
    return (
        await session.execute(select(m.CrewRole.id).where(m.CrewRole.name == name))
    ).scalar_one()


async def upsert_show_cast(
    session: AsyncSession, *, show_id: int, entries: list[TVMazeCastEntry]
) -> None:
    """Replace this show's cast rows. Caller owns the transaction.

    Delete-then-insert, same reasoning as upsert_akas: TV Maze both adds and
    removes entries, and there is no upstream row id to upsert against.
    Insertion follows the upstream array, which is billing order.
    """
    await session.execute(delete(m.ShowCast).where(m.ShowCast.show_id == show_id))
    if not entries:
        return

    await upsert_persons(session, [e.person for e in entries])
    await upsert_characters(session, [e.character for e in entries])

    rows = []
    seen: set[tuple[int, int]] = set()
    for e in entries:
        key = (e.person.id, e.character.id)
        if key in seen:
            continue        # same credit sent twice upstream is one credit
        seen.add(key)
        rows.append(
            {
                "show_id": show_id,
                "person_id": e.person.id,
                "character_id": e.character.id,
                "is_self": e.is_self,
                "is_voice": e.is_voice,
                "sort_order": len(rows),
            }
        )
    for start in range(0, len(rows), _CREDIT_BATCH_SIZE):
        await session.execute(
            insert(m.ShowCast).values(rows[start : start + _CREDIT_BATCH_SIZE])
        )


async def upsert_show_crew(
    session: AsyncSession, *, show_id: int, entries: list[TVMazeCrewEntry]
) -> None:
    """Replace this show's crew rows. Caller owns the transaction."""
    await session.execute(delete(m.ShowCrew).where(m.ShowCrew.show_id == show_id))
    if not entries:
        return

    await upsert_persons(session, [e.person for e in entries])
    role_ids = {t: await resolve_crew_role(session, t) for t in {e.type for e in entries}}

    rows = []
    seen: set[tuple[int, int]] = set()
    for e in entries:
        key = (e.person.id, role_ids[e.type])
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "show_id": show_id,
                "person_id": e.person.id,
                "role_id": role_ids[e.type],
                "sort_order": len(rows),
            }
        )
    for start in range(0, len(rows), _CREDIT_BATCH_SIZE):
        await session.execute(
            insert(m.ShowCrew).values(rows[start : start + _CREDIT_BATCH_SIZE])
        )


async def mark_credits_synced(session: AsyncSession, *, show_id: int) -> None:
    await session.execute(
        update(m.Show).where(m.Show.id == show_id).values(credits_synced_at=datetime.now(UTC))
    )
```

- [ ] Import `TVMazeCastEntry`, `TVMazeCharacter`, `TVMazeCrewEntry`, `TVMazePerson` at the top of `upsert.py`.
- [ ] `task test -- tests/integration/tvmaze/test_credit_upsert.py` passes.

---

## Task 6: ~~Fix `get_last_successful_cursor`~~ — **DONE, shipped as NEU-954**

Landed ahead of this plan. `runs.py` now exposes:

```python
SHOW_CURSOR_KINDS: tuple[str, ...] = ("initial", "update")
PERSON_CURSOR_KINDS: tuple[str, ...] = ("person_initial", "person_update")

async def get_last_successful_cursor(
    session: AsyncSession, *, kinds: Sequence[str] = SHOW_CURSOR_KINDS
) -> int | None: ...
```

**Read this before writing the person delta — the design is not what this plan originally said.** The first draft scoped the lookup to a single `kind == "update"`. That is wrong and the existing suite caught it: `ingest.py:135` finalizes a succeeded **`initial`** run *with* a cursor (`max(updates.values())`, line 46), and the first daily delta inherits it because it has no `update` predecessor of its own. Narrowing to one kind breaks that handoff — the first delta after any initial ingest falls back to `0` and silently re-fetches the whole ~87k-show catalog.

So the cursor is scoped **per axis**, not per kind: `initial` and `update` share one lineage. When Task 19 builds `person_update`, it must pass `kinds=PERSON_CURSOR_KINDS` — and `person_ingest` must finalize with `last_update_cursor` set, exactly as `ingest.py` does, or the person delta will have nothing to inherit on its first run.

Guarded by `test_initial_ingest_cursor_is_inherited_by_the_daily_delta` and `test_get_last_successful_cursor_is_scoped_to_its_axis` in `tests/integration/tvmaze/test_runs.py`.

---

## Task 7: Specials on the ongoing path

**Ticket:** NEU-933

- [ ] In `upsert.py`, add a payload-independent episode upsert entry point so both the embed and the episodes endpoint feed the same code. `upsert_episodes` already takes `list[TVMazeEpisode]`, so no change is needed — only the callers change.
- [ ] In `ingest.py` and `update.py`, replace the embed-derived episode list with the endpoint-derived one:

```python
        # /shows/{id}/episodes?specials=1 returns ALL episodes including
        # specials; embed[]=episodes silently omits them and ignores the
        # specials flag. Use the endpoint as the sole episode source.
        episodes_payload = await client.get_show_episodes(show_id, specials=True)
        episodes = [TVMazeEpisode.model_validate(e) for e in episodes_payload]
```
and pass `episodes` into `upsert_show_payload` (add an `episodes` keyword argument that overrides `show.embedded.episodes` when supplied, defaulting to the embed so existing callers are unaffected).

- [ ] Add a test asserting an unnumbered episode round-trips:

```python
async def test_special_with_null_number_upserts(session):
    ...
    assert ep.number is None
```

- [ ] Audit `number IS NULL` handling downstream — season progress, Watch Next, Upcoming. Grep for `Episode.number` and confirm each site tolerates NULL. **Record findings on NEU-933**; fixing consumer semantics (does a special count toward completion?) is a separate product decision and out of scope here.

---

## Task 8: Pass A orchestrator

**Ticket:** NEU-926

- [ ] Create `src/tvbf/tvmaze/show_refresh.py`, modeled on `akas_backfill.py`. Per-show error handling is identical to that file — same three try/except arms, same consecutive-failure abort — so copy its structure rather than inventing one.

```python
async def run_show_refresh(
    *,
    session_factory: SessionFactory,
    client: Any,  # needs get_show(id, embed=[...]) and get_show_episodes(id, specials=True)
    run_id: UUID,
    failure_threshold: int = 10,
) -> BackfillResult:
    """Pass A: re-fetch every show for cast, crew, externals, ratings, specials.

    Two requests per show. `embed[]=episodes` is deliberately NOT requested —
    /shows/{id}/episodes?specials=1 returns the full list including specials,
    so the embed would be redundant payload.
    """
    async with _owned_session(session_factory) as s:
        todo = (
            (await s.execute(
                select(m.Show.id).where(m.Show.credits_synced_at.is_(None)).order_by(m.Show.id)
            )).scalars().all()
        )
    ...
    for show_id in todo:
        # fetch arm — both calls, either can fail
        try:
            payload = await client.get_show(show_id, embed=["seasons", "cast", "crew"])
            episodes_payload = await client.get_show_episodes(show_id, specials=True)
        except httpx.HTTPStatusError as e:
            ...  # identical to akas_backfill
        # write arm — one transaction per show
        try:
            async with _owned_session(session_factory) as s:
                show = TVMazeShow.model_validate(payload)
                episodes = [TVMazeEpisode.model_validate(e) for e in episodes_payload]
                await upsert_show_payload(s, show, episodes=episodes)
                await upsert_show_cast(s, show_id=show.id, entries=show.embedded.cast)
                await upsert_show_crew(s, show_id=show.id, entries=show.embedded.crew)
                await mark_credits_synced(s, show_id=show.id)
                # NEU-161's backfill is believed complete in prod, but stamping
                # is free and makes a re-run a no-op either way.
                await mark_ratings_synced(s, show_id=show.id)
                await record_progress(s, run_id, processed_delta=1)
                await s.commit()
```

- [ ] Tests in `tests/integration/tvmaze/test_show_refresh.py`, using a `FakeClient` in the style of `test_akas_backfill.py`:
  - processes only shows with `credits_synced_at IS NULL`
  - cast, crew, persons, characters and crew roles all land
  - specials (episodes with `number=None`) land
  - `externals_tvdb` and `rating_average` are written
  - a re-run is a no-op (watermark respected)
  - HTTP failure on either call is non-fatal and increments `shows_failed`
  - N consecutive failures aborts with `status='failed'`
  - **the watermark is not stamped when the write transaction fails** — otherwise a failed show is never retried

---

## Task 9: Admin endpoints + task targets for pass A

**Ticket:** NEU-926

- [ ] In `routers/admin.py`, add `_background_show_refresh` — a verbatim copy of `_background_backfill_akas` with `run_show_refresh` substituted — plus:

```python
@router.post("/refresh-shows", status_code=status.HTTP_202_ACCEPTED)
async def trigger_show_refresh(
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    run_id = await create_run(session, kind="show_refresh")
    await session.commit()
    asyncio.create_task(_background_show_refresh(run_id, settings))
    return {"run_id": str(run_id)}


@router.get("/refresh-shows/{run_id}")
async def get_show_refresh_status(
    run_id: UUID, session: AsyncSession = Depends(get_session)
) -> dict:
    row = (
        await session.execute(select(m.IngestRun).where(m.IngestRun.id == run_id))
    ).scalar_one_or_none()
    if row is None or row.kind != "show_refresh":
        raise HTTPException(status_code=404, detail="run not found")
    return _serialize_run(row)
```

- [ ] Add `refresh:shows` and `refresh:shows:status` to `Taskfile.yml`, copying the `akas:backfill` pair exactly and changing the path.
- [ ] Route tests in the style of `test_admin_routes.py`. **Note the conftest caveat:** admin tests patch `asyncio.create_task`, and an autouse fixture that requests `monkeypatch` reorders teardown and breaks them — don't add one.

---

## Task 10: Backend gate — milestone 1 ingest

- [ ] `task format && task lint && task typecheck && task test`

---

## Task 11: Read schemas + queries for cast and crew

**Ticket:** NEU-940

- [ ] Add to `tvmaze/schemas.py`:

```python
class PersonRef(BaseModel):
    id: int
    name: str
    image_medium: str | None = None


class CharacterRef(BaseModel):
    id: int
    name: str
    image_medium: str | None = None


class CastMemberOut(BaseModel):
    person: PersonRef
    character: CharacterRef
    self_credit: bool = Field(False, serialization_alias="self")
    voice: bool = False


class CrewMemberOut(BaseModel):
    person: PersonRef
    role: str
```

- [ ] Add to `browse_queries.py`:

```python
async def list_show_cast(session: AsyncSession, show_id: int) -> list[Row]:
    stmt = (
        select(m.ShowCast, m.Person, m.Character)
        .join(m.Person, m.Person.id == m.ShowCast.person_id)
        .join(m.Character, m.Character.id == m.ShowCast.character_id)
        .where(m.ShowCast.show_id == show_id)
        .order_by(m.ShowCast.sort_order)
    )
    return (await session.execute(stmt)).all()


async def list_show_crew(session: AsyncSession, show_id: int) -> list[Row]:
    stmt = (
        select(m.ShowCrew, m.Person, m.CrewRole)
        .join(m.Person, m.Person.id == m.ShowCrew.person_id)
        .join(m.CrewRole, m.CrewRole.id == m.ShowCrew.role_id)
        .where(m.ShowCrew.show_id == show_id)
        .order_by(m.ShowCrew.sort_order)
    )
    return (await session.execute(stmt)).all()
```

Both are covered by the `(show_id, sort_order)` indexes. No hydration helper is needed — these are single-show routes, so there's no N+1 of the kind `hydrate_show_refs` exists to batch.

---

## Task 12: Routes — `/shows/{id}/cast`, `/shows/{id}/crew`

**Ticket:** NEU-940

- [ ] Tests first, `tests/integration/routers/test_credits_routes.py`:

```python
async def test_cast_returns_billing_order(client, seeded_show_with_cast):
    r = await client.get("/shows/1/cast")
    assert r.status_code == 200
    assert [c["person"]["name"] for c in r.json()] == ["Lead", "Second", "Third"]


async def test_show_with_no_cast_returns_empty_list_not_404(client, bare_show):
    # 27% of the catalog has zero cast. Empty is normal, not an error.
    r = await client.get("/shows/2/cast")
    assert r.status_code == 200 and r.json() == []


async def test_unknown_show_404s(client):
    assert (await client.get("/shows/999999/cast")).status_code == 404


async def test_cache_header_is_private(client, seeded_show_with_cast):
    r = await client.get("/shows/1/cast")
    assert r.headers["Cache-Control"] == "private, max-age=300"


async def test_requires_auth(unauthenticated_client):
    assert (await unauthenticated_client.get("/shows/1/cast")).status_code == 401
```

- [ ] Add the routes to `routers/browse.py`. They take the **router-level** `_set_browse_cache` dependency (`private, max-age=300`) — do not add a `no-store` override, because these payloads carry no per-user fields.

```python
@router.get("/shows/{show_id}/cast", response_model=list[CastMemberOut])
async def list_show_cast_route(
    show_id: int, session: AsyncSession = Depends(get_session)
) -> list:
    if not await browse_queries.show_exists(session, show_id):
        raise HTTPException(status_code=404, detail="show not found")
    return [build_cast_member(r) for r in await browse_queries.list_show_cast(session, show_id)]
```

- [ ] `show_exists` may already exist in `browse_queries`; reuse it, or add a trivial `SELECT 1` helper. **Distinguishing "no such show" (404) from "show with no cast" ([]) requires this check** — don't let an empty result stand in for a missing show.

---

## Task 13: Frontend — cast and crew on the show page

**Ticket:** NEU-941

- [ ] Add `CastMember`, `CrewMember`, `PersonRef`, `CharacterRef` to `src/api/types.ts`.
- [ ] Add `useShowCast(showId)` / `useShowCrew(showId)` to `src/api/shows.ts`, going through `client.ts`.
- [ ] `src/components/PersonChip.tsx` — headshot + name, with a missing-image fallback (plenty of people have no image). **Render as plain text for now**; Task 27 turns it into a link to `/people/:id`. Keep the swap to one line.
- [ ] `src/components/CastList.tsx` — renders in API order. **Do not sort client-side** — that order is billing order and capturing it is the reason `sort_order` exists.
- [ ] `src/components/CrewList.tsx` — group by role, with a collapse-after-N. Crew averages ~2× cast and reaches 533 on The Simpsons.
- [ ] Wire both into `ShowDetailPage`. **Each section renders nothing at all when empty** — no header, no placeholder row.
- [ ] MSW handlers + vitest covering: populated, cast-but-no-crew, and neither. Assert rendered order matches API order rather than alphabetical.

---

## Task 14: Frontend gate — milestone 1

- [ ] From `tvbf-frontend/`: `task lint && task typecheck && task test`

---

## Task 15: Run pass A in prod

**Ticket:** NEU-938 — see that ticket for the full pre-flight and verification checklist. Not a code task; it is a ~27h operational step and **milestone 2 must not start against prod until it completes.**

---

# Milestone 2 — Person axis

## Task 16: Payloads — guest cast credits

**Ticket:** NEU-942

Guest credits carry `_links`, not embedded objects. Ids come out of href strings.

- [ ] Tests first, `tests/unit/tvmaze/test_person_payloads.py`:

```python
RAW_GUEST = {
    "self": True, "voice": False,
    "_links": {
        "episode": {"href": "https://api.tvmaze.com/episodes/111196",
                    "name": "50 Charades of Grey"},
        "character": {"href": "https://api.tvmaze.com/characters/115733",
                      "name": "Zachary Levi"},
    },
}


def test_guest_credit_parses_ids_out_of_links():
    c = TVMazeGuestCastCredit.model_validate(RAW_GUEST)
    assert c.episode_id == 111196
    assert c.character_id == 115733
    assert c.character_name == "Zachary Levi"
    assert c.is_self is True


def test_guest_credit_with_missing_links_is_skippable():
    c = TVMazeGuestCastCredit.model_validate({"self": False, "voice": False, "_links": {}})
    assert c.episode_id is None and c.character_id is None
```

- [ ] Add to `api_payloads.py`:

```python
def _id_from_href(href: str | None) -> int | None:
    """Trailing path segment of a TV Maze self-link, as an int."""
    if not href:
        return None
    tail = href.rstrip("/").rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else None


class TVMazeGuestCastCredit(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    is_self: bool = Field(False, alias="self")
    is_voice: bool = Field(False, alias="voice")
    links: dict = Field(default_factory=dict, alias="_links")

    @property
    def episode_id(self) -> int | None:
        return _id_from_href((self.links.get("episode") or {}).get("href"))

    @property
    def character_id(self) -> int | None:
        return _id_from_href((self.links.get("character") or {}).get("href"))

    @property
    def character_name(self) -> str | None:
        return (self.links.get("character") or {}).get("name")


class TVMazePersonEmbedded(BaseModel):
    model_config = ConfigDict(extra="ignore")

    guestcastcredits: list[TVMazeGuestCastCredit] = Field(default_factory=list)


class TVMazePersonDetail(TVMazePerson):
    embedded: TVMazePersonEmbedded = Field(
        default_factory=TVMazePersonEmbedded, alias="_embedded"
    )
```

---

## Task 17: Client — person endpoints

**Ticket:** NEU-942

```python
    async def get_person(self, person_id: int) -> dict:
        """Person plus guest credits in one request.

        Only guestcastcredits is embedded. castcredits/crewcredits are free to
        request but are written by the show axis, and person-side credits carry
        no ordering — writing them would clobber billing order.
        """
        url = f"{self._base_url}/people/{person_id}"
        resp = await self._request("GET", url, params=[("embed[]", "guestcastcredits")])
        return resp.json()

    async def get_person_updates(self) -> dict[int, int]:
        url = f"{self._base_url}/updates/people"
        resp = await self._request("GET", url)
        return {int(k): int(v) for k, v in resp.json().items()}
```

---

## Task 18: Upsert — guest cast

**Ticket:** NEU-942

```python
async def upsert_person_guest_cast(
    session: AsyncSession, *, person_id: int, credits: list[TVMazeGuestCastCredit]
) -> None:
    """Replace this PERSON's guest-cast rows. Caller owns the transaction.

    Grain is per-person, not per-episode — guest credits are only reachable
    from the person side, and deleting by episode would wipe other people's
    credits on the same episode.

    Credits whose character or episode id is missing are skipped. Credits
    pointing at episodes we don't mirror will raise on the FK; pass A must
    have populated specials first.
    """
    await session.execute(
        delete(m.EpisodeGuestCast).where(m.EpisodeGuestCast.person_id == person_id)
    )
    usable = [c for c in credits if c.episode_id is not None and c.character_id is not None]
    if not usable:
        return

    await upsert_characters(
        session,
        [TVMazeCharacter(id=c.character_id, name=c.character_name or "") for c in usable],
    )

    rows = []
    seen: set[tuple[int, int]] = set()
    for c in usable:
        key = (c.episode_id, c.character_id)
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "episode_id": c.episode_id,
                "person_id": person_id,
                "character_id": c.character_id,
                "is_self": c.is_self,
                "is_voice": c.is_voice,
                "sort_order": len(rows),
            }
        )
    for start in range(0, len(rows), _CREDIT_BATCH_SIZE):
        await session.execute(
            insert(m.EpisodeGuestCast).values(rows[start : start + _CREDIT_BATCH_SIZE])
        )


async def mark_person_credits_synced(session: AsyncSession, *, person_id: int) -> None:
    await session.execute(
        update(m.Person).where(m.Person.id == person_id)
        .values(credits_synced_at=datetime.now(UTC))
    )
```

- [ ] Test that a guest credit pointing at a **missing episode** raises rather than silently dropping — this is the FK doing its job, and pass C's per-person error handling will count it. If prod shows a nonzero rate of these, specials didn't land and the run should be stopped.

---

## Task 19: Pass C orchestrator + person delta

**Tickets:** NEU-942, NEU-943

- [ ] `src/tvbf/tvmaze/person_ingest.py` — same shape as `show_refresh.py`:

```python
    updates = await client.get_person_updates()
    async with _owned_session(session_factory) as s:
        synced = set(
            (await s.execute(
                select(m.Person.id).where(m.Person.credits_synced_at.is_not(None))
            )).scalars().all()
        )
    todo = sorted(pid for pid in updates if pid not in synced)
```
Per person: `upsert_persons([detail])` → `upsert_person_guest_cast(...)` → `mark_person_credits_synced(...)` → `record_progress(...)`, one transaction each. `kind='person_initial'`.

- [ ] **`person_ingest` must finalize with a cursor**, exactly as `ingest.py:135` does — `max(updates.values())` captured before the loop. Without it the first person delta has nothing to inherit and re-walks all 486k people.

- [ ] `src/tvbf/tvmaze/person_update.py` — mirrors `update.py`:

```python
    async with _owned_session(session_factory) as s:
        cursor = await get_last_successful_cursor(s, kinds=PERSON_CURSOR_KINDS) or 0
    updates = await client.get_person_updates()
    todo = sorted(pid for pid, epoch in updates.items() if epoch > cursor)
    max_epoch = max((updates[pid] for pid in todo), default=cursor)
```
finalizing with `last_update_cursor=max_epoch`. `kind='person_update'`. **Pass `kinds=PERSON_CURSOR_KINDS`, never a bare kind** — see Task 6.

- [ ] Tests for both, in the `FakeClient` style. Cover: watermark respected on re-run; cursor advances only on success; a person whose epoch moved is re-fetched and one whose didn't is not; guest credits are **replaced not appended** on re-fetch; consecutive-failure abort.
- [ ] Admin endpoints `/admin/ingest-people` (+ status) and `/admin/update-people`, plus `people:ingest`, `people:ingest:status`, `people:update` task targets.

---

## Task 20: Person trigram index migration

**Ticket:** NEU-950

```python
def upgrade() -> None:
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_person_name_folded_trgm
        ON tvmaze.person
        USING gin (
            immutable_unaccent(lower(regexp_replace(name, '[[:punct:][:space:]]+', '', 'g')))
            gin_trgm_ops
        )
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS tvmaze.ix_person_name_folded_trgm")
```

`immutable_unaccent` already exists (migration `c2e451aa1ec6`), and `conftest.py` creates it for the test DB.

---

## Task 21: Person queries + search

**Tickets:** NEU-948, NEU-950

- [ ] Reuse `_fold` from `browse_queries.py` **verbatim** — do not re-implement. Folding the token in Python instead of Postgres would let the two sides disagree on exotic characters, which is exactly what the NEU-433 spec warns against.
- [ ] `search_people(session, search, page, per_page)` — same token-AND loop as `list_shows`, same empty-token guard (a query folding to nothing matches nothing, not everything).
- [ ] `get_person(session, person_id)`, `list_person_cast_credits`, `list_person_crew_credits`, `list_person_guest_credits` (joined through `episode` → `show`, ordered by airdate desc), `list_episode_guest_cast(session, episode_id)`.
- [ ] Tests: `visnjic` matches `Goran Višnjić`; `zachary levi` matches and `zachary garcia` doesn't; punctuation-only returns nothing; **show search results are unchanged** (explicit regression test — `list_shows` must not be touched).

---

## Task 22: Person + guest-cast routes

**Tickets:** NEU-948, NEU-949, NEU-950

- [ ] `GET /people?search=&page=&per_page=`, `GET /people/{id}`, `GET /people/{id}/credits`, `GET /episodes/{id}/guest-cast` on the browse router.
- [ ] `/people/{id}/credits` returns `{cast, crew, guest_cast}` — **always all three keys, empty arrays when a category is absent**, never omitted keys.
- [ ] Guest credit entries carry episode id, name, season, number **and** parent show id + name, so the frontend renders "Show — S2E11" without a second round trip.
- [ ] Tests: all-three-populated; only-one-populated; person with no credits (200, three empty arrays); unknown id 404; episode with no guest cast returns `[]` and 200.

---

## Task 23: Backend gate — milestone 2

- [ ] `task format && task lint && task typecheck && task test`

---

## Task 24: Frontend — person page

**Ticket:** NEU-951

- [ ] `src/api/people.ts` with `usePersonSearch`, `usePerson`, `usePersonCredits`.
- [ ] `src/pages/PersonPage.tsx` — header (headshot, name, dates, country) plus three sections. Expect wildly uneven sizes: Zachary Levi is 11 / 0 / 61. Each section hides when empty; the guest section usually needs collapsing or paging.
- [ ] Register `/people/:id` in `router.tsx`.
- [ ] **Flip `PersonChip` to a link.** If it isn't a one-line change, fix the component here.

---

## Task 25: Frontend — episode guest cast

**Ticket:** NEU-952

- [ ] Reuse `CastList` — the payload shape is identical to show cast.
- [ ] **96% of episodes have zero guest cast.** Assert on *absence* in tests, not on an empty container. Consider deferring the fetch so the common case costs nothing.

---

## Task 26: Frontend — People section in search

**Ticket:** NEU-953

- [ ] `SearchOverlay` becomes sectioned. This is the real cost of separate-entity search:
  - arrow keys traverse Shows → People continuously; Enter activates whichever is focused
  - a section with no results hides; the overlay's "no results" state appears only when **both** are empty
  - two in-flight requests per keystroke, sharing debounce and cancellation; a slow one must not block the other from rendering
- [ ] Tests: both populated, only shows, only people, neither, and keyboard traversal across the boundary.

---

## Task 27: Frontend gate — milestone 2

- [ ] From `tvbf-frontend/`: `task lint && task typecheck && task test`

---

## Task 28: Run pass C in prod

**Ticket:** NEU-944 — ~75h. See that ticket for pre-flight and verification. The sharpest check: **Zachary Levi (person 30856) should have 11 cast credits and 61 guest credits.** Near-zero guest credits means the `_links` href parsing is wrong.

---

## Manual smoke test

```bash
# from tvbf-backend/
task up && task migrate
task test

# pass A against a handful of shows (point TVMAZE_BASE_URL at a fixture server,
# or run with a seeded DB where only a few shows have credits_synced_at IS NULL)
task refresh:shows
task refresh:shows:status -- <uuid>

curl -sk https://api.tvbf.localhost/shows/168/cast -b "$COOKIE" | jq '.[0]'
curl -sk https://api.tvbf.localhost/shows/168/crew -b "$COOKIE" | jq 'length'

# milestone 2
task people:ingest
task people:ingest:status -- <uuid>
curl -sk "https://api.tvbf.localhost/people?search=levi" -b "$COOKIE" | jq
curl -sk https://api.tvbf.localhost/people/30856/credits -b "$COOKIE" | jq 'keys'
```

Then in the browser at `https://app.tvbf.localhost`: open a show with cast (168), one without (any of the 27%), an episode with guest cast, an episode without, a person page, and the search overlay with a query matching both a show and a person.

---

## Self-review checklist

- [ ] No unique constraint on any of the three credit tables.
- [ ] `sort_order` populated from upstream array index everywhere; **no client-side re-sorting** of cast.
- [ ] `episode_guest_cast` deleted `WHERE person_id = ?`, never by episode.
- [ ] Pass C requests **only** `guestcastcredits` — never `castcredits`/`crewcredits`.
- [ ] `get_last_successful_cursor` passes `kinds=PERSON_CURSOR_KINDS` on the person axis; no call site uses a bare kind.
- [ ] `person_ingest` finalizes with `last_update_cursor` set, so the first person delta inherits it.
- [ ] `ratings_backfill.py`'s `get_show` call updated for the now-real `embed` argument.
- [ ] Watermarks stamped only inside the successful write transaction.
- [ ] `OptionalDate` used for `person.birthday` / `deathday`.
- [ ] Every new route uses the router-level `private, max-age=300`; none adds `no-store`.
- [ ] Empty cast/crew/guest-cast returns `[]` + 200; only a missing parent is 404.
- [ ] Route tests use `AsyncClient(ASGITransport(app=app))`, never `TestClient`.
- [ ] No autouse fixture requests `monkeypatch`.
- [ ] Batch constants exercised by a >1,000-row test.
- [ ] `task format` run before staging.
