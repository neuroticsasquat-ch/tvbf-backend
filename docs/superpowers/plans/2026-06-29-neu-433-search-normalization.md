# Search Text Normalization (accents + punctuation) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Spec:** `docs/superpowers/specs/2026-06-29-neu-433-search-normalization-design.md`
**Tickets:** NEU-433 (diacritics), NEU-434 (hyphenated titles) — both closed by this one change.

**Goal:** Make browse search accent- and punctuation-insensitive so `shogun` matches `Shōgun` and `spiderman` matches `Spider-Man`, without breaking non-Latin native-title search.

**Architecture:** Introduce one SQL fold expression — `unaccent(lower(regexp_replace(x, '[[:punct:][:space:]]+', '', 'g')))` — applied symmetrically to the searched column and to each query token in `browse_queries.py`. Add the `unaccent` Postgres extension via an Alembic migration (dev/prod) and a conftest line (test DB). No frontend change; the browse API response shape is unchanged.

**Tech Stack:** FastAPI, async SQLAlchemy 2.x, Postgres (`unaccent` extension), Alembic, pytest + pytest-asyncio (session-scoped loop), `AsyncClient(ASGITransport)`.

## Global Constraints

- **Backend only.** All paths are under `tvbf-backend/`. All `python`/`pytest`/`alembic` commands run **inside the container** via `task` targets — there is no local toolchain.
- **No git commit steps.** Per this repo's plan convention, the user commits on their own cadence. Do not run any state-changing git command.
- **Modern type hints** (`X | None`), no `from __future__ import annotations`.
- **The fold regex is `[[:punct:][:space:]]+`, NOT `[^a-z0-9]`.** The latter would delete CJK/Cyrillic letters and break native-title search.
- **`unaccent` is added in two places:** the Alembic migration (covers dev/prod) AND `tests/conftest.py::test_engine` (the test DB is built by `Base.metadata.create_all` + explicit `CREATE EXTENSION` calls, not by migrations). Both are required.
- Tests are `async def`, use the session-scoped `session` fixture, and seed via `session.add(...)` / `insert(...)`. Multiple inserts with FK dependencies need an explicit `await session.flush()` between parent and child (no `relationship()` in models).
- Run gates inside the container: `task test -- <path>`, `task lint`, `task typecheck`.

---

### Task 1: Add the `unaccent` extension (migration + test DB)

Installs the `unaccent` extension everywhere the code will need it. This is a prerequisite for every fold query in later tasks. The test asserts the extension is actually present in `tvbf_test`.

**Files:**
- Create: `migrations/versions/<generated>_create_unaccent_extension.py`
- Modify: `tests/conftest.py` (the `test_engine` fixture, around line 37)
- Test: `tests/integration/tvmaze/test_search_normalization.py` (new file)

**Interfaces:**
- Produces: the `unaccent(text) -> text` SQL function available in both the dev/prod databases and `tvbf_test`. Later tasks call it via `func.unaccent(...)`.

- [ ] **Step 1: Write the failing test**

Create `tests/integration/tvmaze/test_search_normalization.py`:

```python
from sqlalchemy import insert, text

from tvbf.tvmaze import models as m
from tvbf.tvmaze.browse_queries import hydrate_matched_aka, list_shows
from tvbf.tvmaze.schemas import ShowFilters


async def test_unaccent_extension_available(session):
    result = await session.execute(text("SELECT unaccent('Shōgun')"))
    assert result.scalar_one() == "Shogun"
```

(The unused imports are consumed by later tasks' tests in this same file; if your linter blocks the run before Task 3, add them as those tests are written instead.)

- [ ] **Step 2: Run the test to verify it fails**

Run: `task test -- tests/integration/tvmaze/test_search_normalization.py::test_unaccent_extension_available`
Expected: FAIL — `function unaccent(unknown) does not exist` (extension not yet installed in `tvbf_test`).

- [ ] **Step 3: Add the extension to the test DB**

In `tests/conftest.py`, inside the `test_engine` fixture, add a line next to the existing extension creations (after the `pgcrypto` line, ~line 37):

```python
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS citext"))
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS unaccent"))
```

- [ ] **Step 4: Create the Alembic migration (covers dev/prod)**

Generate an empty migration so the revision/down-revision are wired to the current head automatically:

Run: `task makemigration -- "create unaccent extension"`

Then replace the body of the generated file `migrations/versions/<generated>_create_unaccent_extension.py` so `upgrade`/`downgrade` read exactly:

```python
def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS unaccent")


def downgrade() -> None:
    # Leave the extension installed — harmless to keep and other objects may
    # depend on it. No-op downgrade.
    pass
```

Leave the auto-generated `revision`, `down_revision`, `branch_labels`, `depends_on` header lines untouched. Remove any autogenerated `op.*` lines in the body other than the ones above (an empty autogenerate may leave `pass` or nothing — that's fine to replace).

- [ ] **Step 5: Verify the migration applies cleanly against the dev DB**

Run: `task migrate`
Expected: `Running upgrade <prev> -> <generated>, create unaccent extension` with no error. Re-running `task migrate` is a no-op (idempotent).

- [ ] **Step 6: Run the test to verify it passes**

Run: `task test -- tests/integration/tvmaze/test_search_normalization.py::test_unaccent_extension_available`
Expected: PASS.

- [ ] **Step 7: Confirm migration history has a single head**

Run: `task shell` then `alembic heads` (or `docker compose exec tvbf-backend alembic heads`).
Expected: exactly one head — the new revision. If two heads appear, the `down_revision` is wrong; fix it to point at the prior head.

---

### Task 2: Fold helpers + accent/punctuation-aware `list_shows` search

Adds the shared fold expression and the Python empty-token guard, then rewrites the `list_shows` search loop to fold both the column and the token. This is the change that fixes NEU-433 and NEU-434 for the main search path.

**Files:**
- Modify: `src/tvbf/tvmaze/browse_queries.py` (imports at top; new helpers; `list_shows` search block ~lines 132-141)
- Test: `tests/integration/tvmaze/test_search_normalization.py` (append)

**Interfaces:**
- Produces:
  - `_fold(expr)` — returns a SQLAlchemy expression `func.unaccent(func.lower(func.regexp_replace(expr, "[[:punct:][:space:]]+", "", "g")))`. Accepts either a column (e.g. `m.Show.name`) or a bound value (e.g. `literal(token)`).
  - `_strip_punct_space(token: str) -> str` — Python helper returning the token with Unicode punctuation/separator/whitespace characters removed.
- Consumes: the `unaccent` function from Task 1.

- [ ] **Step 1: Write the failing tests**

Append to `tests/integration/tvmaze/test_search_normalization.py`:

```python
async def test_search_matches_accented_title_without_accents(session):
    session.add(m.Show(id=70001, name="Shōgun", tvmaze_updated=1))
    await session.commit()
    rows, total = await list_shows(
        session, ShowFilters(search="shogun"), sort="name", page=1, per_page=20
    )
    assert {r.id for r in rows} == {70001}
    assert total == 1


async def test_search_matches_hyphenated_title_as_one_word(session):
    session.add(m.Show(id=70002, name="Spider-Man", tvmaze_updated=1))
    await session.commit()
    rows, _ = await list_shows(
        session, ShowFilters(search="spiderman"), sort="name", page=1, per_page=20
    )
    assert {r.id for r in rows} == {70002}


async def test_search_multitoken_across_punctuation_still_matches(session):
    session.add(m.Show(id=70010, name="Alien: Earth", tvmaze_updated=1))
    session.add(m.Show(id=70011, name="The Office (US)", tvmaze_updated=1))
    await session.commit()
    rows, _ = await list_shows(
        session, ShowFilters(search="alien earth"), sort="name", page=1, per_page=20
    )
    assert 70010 in {r.id for r in rows}
    rows2, _ = await list_shows(
        session, ShowFilters(search="the office us"), sort="name", page=1, per_page=20
    )
    assert 70011 in {r.id for r in rows2}


async def test_search_preserves_non_latin_native_titles(session):
    session.add(m.Show(id=70020, name="進撃の巨人", tvmaze_updated=1))
    await session.commit()
    rows, _ = await list_shows(
        session, ShowFilters(search="進撃"), sort="name", page=1, per_page=20
    )
    assert {r.id for r in rows} == {70020}


async def test_search_punctuation_only_query_returns_nothing(session):
    session.add(m.Show(id=70030, name="Whatever", tvmaze_updated=1))
    await session.commit()
    rows, total = await list_shows(
        session, ShowFilters(search="--"), sort="name", page=1, per_page=20
    )
    assert rows == []
    assert total == 0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `task test -- tests/integration/tvmaze/test_search_normalization.py`
Expected: the accent test fails (`Shōgun` not matched by `shogun`), the hyphen test fails (`Spider-Man` not matched by `spiderman`), and the punctuation-only test fails (currently returns the whole catalog, so `rows != []`). The multitoken and non-Latin tests may already pass under the old code — that's fine, they're regression guards.

- [ ] **Step 3: Add imports and helpers**

In `src/tvbf/tvmaze/browse_queries.py`, update the top-of-file imports:

```python
import unicodedata
from uuid import UUID

from sqlalchemy import false, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
```

Then add these two helpers just above `async def list_shows(` (after the `_SORT_EXPRS` block / the small `list_*` helpers is fine — keep them module-level):

```python
def _fold(expr):
    """Accent- and punctuation-folded form of a text SQL expression or value.

    Strips punctuation and whitespace (preserving letters of every script),
    lowercases, then removes diacritics via the ``unaccent`` extension. Applied
    identically to the searched column and to the query token so both sides of
    the comparison normalize under the same rules — "shogun" matches "Shōgun"
    and "spiderman" matches "Spider-Man", while non-Latin scripts pass through
    unchanged so native-title search keeps working.
    """
    stripped = func.regexp_replace(expr, "[[:punct:][:space:]]+", "", "g")
    return func.unaccent(func.lower(stripped))


def _strip_punct_space(token: str) -> str:
    """Token with punctuation and whitespace removed. Used only to detect tokens
    that fold to nothing (e.g. "--"): ``unaccent`` never maps a non-empty letter
    to empty, so emptiness depends solely on the punctuation/space strip."""
    return "".join(
        c
        for c in token
        if not (unicodedata.category(c)[0] in ("P", "Z") or c.isspace())
    )
```

- [ ] **Step 4: Rewrite the `list_shows` search block**

Replace the existing search block in `list_shows`:

```python
    if filters.search:
        # Token-based AND match: every whitespace-separated token must appear
        # as a substring (case-insensitive) of the show name OR any of its AKAs.
        # Lets "alien earth" match "Alien: Earth", "the office us" match
        # "The Office (US)", and "tokyo revengers" match a Japanese-titled show
        # whose English AKA is "Tokyo Revengers".
        for token in filters.search.split():
            needle = f"%{token}%"
            aka_subq = select(m.ShowAka.show_id).where(m.ShowAka.name.ilike(needle))
            base = base.where(or_(m.Show.name.ilike(needle), m.Show.id.in_(aka_subq)))
```

with:

```python
    if filters.search:
        # Token-based AND match against an accent- and punctuation-folded form of
        # the show name OR any of its AKAs. Folding both the column and the token
        # lets "shogun" match "Shōgun" and "spiderman" match "Spider-Man", while
        # whitespace tokenization keeps "alien earth" matching "Alien: Earth" and
        # non-Latin titles ("進撃") still match natively.
        usable = [t for t in filters.search.split() if _strip_punct_space(t)]
        if not usable:
            # Search was all punctuation/whitespace — match nothing, not everything.
            base = base.where(false())
        for token in usable:
            needle = func.concat("%", _fold(literal(token)), "%")
            aka_subq = select(m.ShowAka.show_id).where(_fold(m.ShowAka.name).like(needle))
            base = base.where(or_(_fold(m.Show.name).like(needle), m.Show.id.in_(aka_subq)))
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `task test -- tests/integration/tvmaze/test_search_normalization.py`
Expected: all tests in the file PASS.

- [ ] **Step 6: Run the existing browse-query and AKA search suites for regressions**

Run: `task test -- tests/integration/tvmaze/test_browse_queries.py tests/integration/test_browse_aka_search.py tests/integration/routers/test_browse.py`
Expected: all PASS. In particular `test_list_shows_search_substring_case_insensitive`, `test_list_shows_search_tokens_match_across_punctuation`, `test_list_shows_search_collapses_extra_whitespace`, and the Japanese native case `test_search_returns_only_shows_matching_native_when_no_aka` stay green.

---

### Task 3: Fold parity in `hydrate_matched_aka`

`hydrate_matched_aka` decides whether a result row's "matched via AKA" badge is shown. It must fold identically to `list_shows`, or a folded match (e.g. `spiderman` → `Spider-Man` carried by the show's own name) could be misattributed to an AKA. The "did the name carry the match" check is moved into SQL so it uses the exact same Postgres `unaccent` rules.

**Files:**
- Modify: `src/tvbf/tvmaze/browse_queries.py` (`hydrate_matched_aka`, ~lines 171-211)
- Test: `tests/integration/tvmaze/test_search_normalization.py` (append)

**Interfaces:**
- Consumes: `_fold`, `_strip_punct_space` from Task 2.
- Produces: `hydrate_matched_aka(session, shows, search)` unchanged signature — returns `dict[int, str | None]` (show_id → matched AKA, or `None` when the show's own folded name carried the match, empty dict when no search/shows).

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/tvmaze/test_search_normalization.py`:

```python
async def test_hydrate_matched_aka_folds_accented_aka(session):
    session.add(m.Show(id=70040, name="進撃の巨人", tvmaze_updated=1))
    await session.flush()
    await session.execute(
        insert(m.ShowAka).values(
            show_id=70040,
            name="Attack on Titan",
            country_code="US",
            country_name="United States",
            language="en",
        )
    )
    await session.commit()

    shows, _ = await list_shows(
        session, ShowFilters(search="attack titan"), sort="name", page=1, per_page=20
    )
    matched = await hydrate_matched_aka(session, shows, search="attack titan")
    assert matched == {70040: "Attack on Titan"}


async def test_hydrate_matched_aka_none_when_folded_name_matches(session):
    """A hyphen-folded match carried by the show's own name reports no AKA badge,
    even when the show also has an AKA that would match."""
    session.add(m.Show(id=70041, name="Spider-Man", tvmaze_updated=1))
    await session.flush()
    await session.execute(
        insert(m.ShowAka).values(
            show_id=70041,
            name="Spiderman (US)",
            country_code="US",
            country_name="United States",
            language="en",
        )
    )
    await session.commit()

    shows, _ = await list_shows(
        session, ShowFilters(search="spiderman"), sort="name", page=1, per_page=20
    )
    matched = await hydrate_matched_aka(session, shows, search="spiderman")
    assert matched == {70041: None}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `task test -- tests/integration/tvmaze/test_search_normalization.py::test_hydrate_matched_aka_folds_accented_aka tests/integration/tvmaze/test_search_normalization.py::test_hydrate_matched_aka_none_when_folded_name_matches`
Expected: FAIL — the old `ilike`-based AKA query and the Python `name_lower` check don't fold accents/punctuation, so the accented-AKA case returns `{}`/wrong and the `Spider-Man` name-match case misattributes to the AKA (`{70041: "Spiderman (US)"}`).

- [ ] **Step 3: Rewrite `hydrate_matched_aka`**

Replace the body of `hydrate_matched_aka` (keep the existing docstring) from the `tokens = search.split()` line through the final `return result` with:

```python
    tokens = [t for t in search.split() if _strip_punct_space(t)]
    if not tokens:
        return {}

    show_ids = [s.id for s in shows]

    # Best (shortest) AKA per show that matches every folded token.
    aka_query = select(m.ShowAka.show_id, m.ShowAka.name).where(m.ShowAka.show_id.in_(show_ids))
    for token in tokens:
        needle = func.concat("%", _fold(literal(token)), "%")
        aka_query = aka_query.where(_fold(m.ShowAka.name).like(needle))
    aka_rows = (await session.execute(aka_query)).all()
    best_by_show: dict[int, str] = {}
    for sid, aname in aka_rows:
        if sid not in best_by_show or len(aname) < len(best_by_show[sid]):
            best_by_show[sid] = aname

    # Which shows matched on their own (folded) name? Determined in SQL so the
    # rule is identical to list_shows — a Python unaccent would diverge on
    # characters like ł/ø that NFKD does not decompose.
    name_query = select(m.Show.id).where(m.Show.id.in_(show_ids))
    for token in tokens:
        needle = func.concat("%", _fold(literal(token)), "%")
        name_query = name_query.where(_fold(m.Show.name).like(needle))
    name_matched_ids = set((await session.execute(name_query)).scalars().all())

    result: dict[int, str | None] = {}
    for show in shows:
        if show.id in name_matched_ids:
            result[show.id] = None
        else:
            result[show.id] = best_by_show.get(show.id)
    return result
```

This removes the now-unused `lower_tokens` / `name_lower` logic.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `task test -- tests/integration/tvmaze/test_search_normalization.py`
Expected: all PASS.

- [ ] **Step 5: Run the full AKA-search regression suite**

Run: `task test -- tests/integration/test_browse_aka_search.py`
Expected: all PASS — especially `test_hydrate_matched_aka_populates_when_only_aka_matches`, `test_hydrate_matched_aka_is_none_when_show_name_matches`, and `test_hydrate_matched_aka_picks_one_per_show_when_multiple_akas_match`.

---

### Task 4: Full gates

Confirm the whole change is green end-to-end before handing back.

**Files:** none (verification only).

- [ ] **Step 1: Full test suite**

Run: `task test`
Expected: all PASS.

- [ ] **Step 2: Lint**

Run: `task lint`
Expected: no errors. (Confirm `unicodedata`, `false`, `literal` are all used and no import is left dangling.)

- [ ] **Step 3: Type check**

Run: `task typecheck`
Expected: no new errors. `_fold` is intentionally untyped on its parameter (it accepts both columns and bound literals); if pyright complains, annotate the return as needed but leave the parameter permissive (`expr` with no annotation, or a broad SQLAlchemy element type).

- [ ] **Step 4: Confirm a single migration head**

Run: `docker compose exec tvbf-backend alembic heads`
Expected: exactly one head (the `create_unaccent_extension` revision).

---

## Self-Review

**Spec coverage:**
- Fold expression `[[:punct:][:space:]]+` + lower + unaccent → Task 2 `_fold`. ✓
- `unaccent` migration (dev/prod) + conftest line (test DB) → Task 1. ✓
- `list_shows` folding both sides, plain `LIKE` → Task 2. ✓
- Empty-token guard / punctuation-only → nothing → Task 2 (`usable` + `false()`), test in Task 2. ✓
- Non-Latin preservation → Task 2 regression test + existing AKA suite (Task 2 Step 6). ✓
- `hydrate_matched_aka` SQL-parity name check → Task 3. ✓
- Acceptance criteria (`shogun`, `spiderman`, `alien earth`, `the office us`, `進撃`, AKA fold, `--`) → covered across Task 2/3 tests. ✓

**Placeholder scan:** No TBD/TODO; every code step shows full code; every run step states expected output. ✓

**Type consistency:** `_fold` / `_strip_punct_space` signatures match between definition (Task 2) and use (Tasks 2, 3). `func.concat("%", _fold(literal(token)), "%")` needle form is identical in `list_shows`, the AKA query, and the name query. `hydrate_matched_aka` return type `dict[int, str | None]` unchanged. ✓
