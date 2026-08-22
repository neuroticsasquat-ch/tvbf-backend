# Search text normalization (accents + punctuation)

- **Tickets:** NEU-433 (diacritics break search — `shogun ≠ shōgun`), NEU-434 (hyphenated titles — `spiderman ≠ spider-man`)
- **Date:** 2026-06-29
- **Status:** Approved
- **Scope:** `tvbf-backend` only. One module changed plus one migration. No frontend change.

## Problem

Browse search compares the user's query against **raw** `tvmaze.show.name` and `tvmaze.show_aka.name` with `ILIKE '%token%'`. `ILIKE` folds case but nothing else, so:

- **Accents (NEU-433):** the needle `%shogun%` never matches `Shōgun` — `o ≠ ō`.
- **Punctuation (NEU-434):** the needle `%spiderman%` is not a substring of `Spider-Man` — the hyphen sits between `r` and `m`.

Both are the same root defect: the comparison runs against un-normalized text. They are fixed together by one change.

The two search sites both live in `src/tvbf/tvmaze/browse_queries.py`:

- `list_shows` — the actual filter (the search loop, ~line 138).
- `hydrate_matched_aka` — decides, per result row, whether to surface the "matched via AKA" badge (~line 194, plus the Python `name_lower` / `all(t in …)` check ~line 207).

If only `list_shows` is fixed, a show can appear in results while `hydrate_matched_aka` reports no match — the badge logic must fold identically.

## Approach

One fold expression, applied symmetrically to the indexed column and to each query token:

```
fold(x) = unaccent(lower(regexp_replace(x, '[[:punct:][:space:]]+', '', 'g')))
```

- `regexp_replace(…, '[[:punct:][:space:]]+', '', 'g')` — strip punctuation and whitespace only (hyphens, colons, parentheses, spaces). `Spider-Man` → `SpiderMan`, `Alien: Earth` → `AlienEarth`.
- `lower` — case fold.
- `unaccent` — strip diacritics. `Shōgun` → `shogun`. Requires the Postgres `unaccent` extension.

**Why `[[:punct:][:space:]]`, not `[^a-z0-9]`.** Stripping everything except ASCII alphanumerics would delete CJK / Cyrillic / any non-Latin letters, breaking native-title search. The repo already has a passing test for Japanese native search (`進撃` → `進撃の巨人`). Removing *only* punctuation and whitespace preserves letters of every script; `unaccent` then folds Latin diacritics and passes non-Latin characters through unchanged. So `進撃の巨人` stays `進撃の巨人` and still matches `進撃`.

The match becomes:

```
fold(name) LIKE '%' || fold(token) || '%'
```

Plain `LIKE`, not `ILIKE` — both sides are already lowercased by `fold`, so case-insensitive matching is redundant.

### Why fold both sides in SQL

The token side is a literal we hold in Python, so we *could* fold it in Python (`unicodedata` + regex). We deliberately do **not**: Python's Unicode folding and Postgres `unaccent` can disagree on exotic characters, which would make the column side and token side asymmetric. Folding the bind parameter through the same Postgres `unaccent(...)` expression guarantees identical rules on both sides.

### Tokenization unchanged

The query is still split on whitespace first (`filters.search.split()`), then each token is folded and substring-tested with AND semantics across tokens. This preserves existing multi-token behavior:

- `alien earth` → tokens `alien`, `earth` → each a substring of `alienearth` (fold of `Alien: Earth`). Still matches.
- `the office us` → `the`, `office`, `us` → all substrings of `theofficeus` (fold of `The Office (US)`). Still matches.
- `進撃` → token `進撃` → substring of `進撃の巨人` (unchanged by fold). Still matches.

### Empty-token guard

A token that folds to empty (e.g. the query `--` or `:::`) would produce the needle `%%`, matching every row. Tokens whose stripped form is empty are dropped before building the `WHERE` clause. If a non-empty `search` was supplied but *every* token strips to empty, the search matches nothing (`WHERE false`) rather than returning the entire catalog — a punctuation-only query is treated as "no results," not "no filter."

## Components

All changes in `src/tvbf/tvmaze/browse_queries.py` except the migration.

1. **`_fold(expr)` helper** — module-level function returning the SQLAlchemy expression `func.unaccent(func.lower(func.regexp_replace(expr, '[[:punct:][:space:]]+', '', 'g')))`. Used for both the column side (passing the column) and the token side (passing a bind literal). Single source of truth so the two sides cannot drift.

2. **`_strip_punct_space(token)` helper** — Python, returns the token with punctuation + whitespace removed (no accent folding needed: `unaccent` never maps a non-empty letter to empty, so emptiness depends only on the punct/space strip). Used solely to detect tokens that fold to nothing.

3. **`list_shows` search loop** — for each token whose `_strip_punct_space` is non-empty, build `_fold(m.Show.name).like(folded_needle)` OR `m.Show.id.in_(aka_subq)` where the AKA subquery filters on `_fold(m.ShowAka.name).like(folded_needle)`. `folded_needle` is `func.concat('%', _fold(literal(token)), '%')`. Tokens that strip to empty are skipped.

4. **`hydrate_matched_aka`** — apply `_fold` to the AKA `LIKE` the same way (skipping empty tokens via `_strip_punct_space`). Replace the Python-side "does the show's own name carry the match" check (currently `all(t in name_lower for t in lower_tokens)`) with a SQL query that selects show ids where `_fold(m.Show.name)` matches every folded token. This guarantees the badge decision uses the *identical* Postgres `unaccent` rules as `list_shows` — a Python re-implementation of `unaccent` would diverge on characters like `ł`/`ø` that NFKD doesn't decompose. Result per show: `None` if its id is in the name-matched set, else the show's best (shortest) matching AKA.

4. **Migration** — one Alembic revision: `op.execute("CREATE EXTENSION IF NOT EXISTS unaccent")` in `upgrade`. `downgrade` may `DROP EXTENSION IF EXISTS unaccent` or be a no-op (the extension is harmless to leave; prefer a no-op downgrade to avoid breaking a shared DB). Idempotent; the existing `tvmaze`/`app` schemas are already present.

## Out of scope (deferred)

- **Stored normalized columns** (`name_folded` generated columns on `show` / `show_aka`).
- **Trigram / GIN indexes** on the folded expression.

Search is already a non-indexed `ILIKE '%…%'` sequential scan today; wrapping the column in `unaccent(lower(regexp_replace(...)))` keeps it a sequential scan at the same order of cost. This is acceptable at the current catalog size (≤ ~80k shows). Revisit indexing only when measured to matter — consistent with the repo's "defer until measured" posture on browse caching/ETags.

## Acceptance criteria

- `shogun` returns `Shōgun`.
- `spiderman` returns `Spider-Man`.
- Regression: `alien earth` still returns `Alien: Earth`.
- Regression: `the office us` still returns `The Office (US)`.
- Regression: non-Latin native search still works — `進撃` still returns `進撃の巨人` (the existing `test_browse_aka_search` cases must stay green).
- An AKA-only match (e.g. an English query against a foreign-titled show whose accented/punctuated English AKA folds to the query) still sets the "matched via AKA" context correctly.
- A punctuation-only query (`--`) returns nothing, not the whole catalog.

## Testing

Integration tests in `tests/integration/tvmaze/` (browse-query layer), seeded against `tvbf_test`:

- Accent match: seed a show named `Shōgun`, assert `search=shogun` returns it.
- Punctuation match: seed `Spider-Man`, assert `search=spiderman` returns it.
- Multi-token regression: `Alien: Earth` via `search=alien earth`; `The Office (US)` via `search=the office us`.
- AKA fold: seed a show with an accented/punctuated AKA, assert it returns for the folded query **and** `hydrate_matched_aka` reports the matched AKA.
- Empty-fold guard: `search=--` returns no rows (not the full catalog).
- The pre-existing `tests/integration/test_browse_aka_search.py` suite (including the Japanese native-title case) must continue to pass unchanged.

The `unaccent` extension must exist in `tvbf_test` for these to pass. The test DB is **not** built from Alembic migrations — `tests/conftest.py::test_engine` uses `Base.metadata.create_all` plus explicit `CREATE EXTENSION IF NOT EXISTS …` calls (today: `citext`, `pgcrypto`). So the extension must be added there too: `CREATE EXTENSION IF NOT EXISTS unaccent` alongside the existing two. The Alembic migration covers the dev/prod databases; the conftest line covers the test database. Both are required.

## Risks / notes

- **`unaccent` is not `IMMUTABLE`.** That only blocks using it in a generated column or an index expression without an `IMMUTABLE` wrapper. We use it in a `WHERE` clause at query time, where `STABLE` is fine — no wrapper needed. (This is exactly why the stored-column option was deferred: it would require the wrapper.)
- **Performance:** per-row `unaccent(regexp_replace(...))` over the candidate set. No worse asymptotically than today's `ILIKE '%…%'`. Fine at current scale.
- The change is backend-internal; the browse API response shape is unchanged, so no frontend work.
