# NEU-1513 — Popularity as a show sort, and the people order (backend half)

**Ticket:** [NEU-1513](https://linear.app/neuroticsasquatch/issue/NEU-1513/add-popularity-if-available-as-default-show-search-sort-option)
**Repo:** `tvbf-backend` — branch `tom/neu-1513-add-popularity-if-available-as-default-show-search-sort` from `main`
**Project:** tvbf: Maintenance
**Other half:** `tvbf-frontend/docs/specs/NEU-1513-popularity-default-search-sort.md` (the overlay option and the default). The frontend half **depends on this one**: it sends `sort=-popularity`, which this half teaches the API. Ship this first.
**Precedents this consumes:** `docs/specs/NEU-1502-speed-up-show-search.md` §2.4 (the stored `last_aired` column and its partial index — the shape every decision here copies), `src/tvbf/tmdb/popularity.py` and `.claude/docs/patterns-tmdb-ingest.md` §popularity (NEU-1172: how `catalog.show.popularity` is kept fresh), `tmdb/recommendations_backfill.py` (the one existing `popularity DESC` ordering, and why it coalesces nulls), ADR-0013 §"Ambiguity resolves by popularity"
**Glossary:** `CONTEXT.md` — **Popularity** (added by this ticket's design session; note what the entry says to *avoid*)
**Status:** approved for implementation

---

## 1. The ticket's two questions, answered

The ticket asks whether TMDB popularity is in our data and what a backfill
would cost. **It is, and there is nothing to backfill.**

- `catalog.show.popularity` (`Double`, nullable) has been mirrored since the
  TMDB cutover and is refreshed nightly from the id export by the catalog
  delta (NEU-1172, `tmdb/popularity.py`). Measured on the workspace catalog
  on 2026-10-01: **230,582 of 230,582 live shows carry a score**; median 1.1,
  p90 4.7, p99 22, max 607; 56,620 distinct values, 18 exact zeros.
- `catalog.person.popularity` (`Double`, nullable) is written by
  `upsert._write_credits` from each show's `aggregate_credits` payload.
  **1,103,345 of 1,103,345 people carry a score**; median 0.36, p99 1.8,
  max 74; 25,265 distinct values, 7,590 zeros. There is no nightly refresh
  for people — a person's score moves only when a show crediting them is
  re-mirrored — so person scores are of mixed vintage. **Accepted** in the
  design session: it is still the right order for a search box, and the
  alternative (TMDB's person export) is a separate ticket if it ever matters.

The premise holds on real data. `office` by popularity puts *The Office* (US,
149.6) first, then *Office Boy*, the 2026 isekai office anime, *The Office
Blanik*, *Revenge Office*, and the UK *The Office*. By the current default,
last aired, the top five are *Front Office Sports Tonight*, a podcast about The
Office, *Office Workers*, *Behind Bars: Officer Cam* and *Deepoffice*. For
people, `carell` by popularity leads with Steve Carell; by name it leads with
*Aancod Abe Zaccarelli*.

## 2. Decisions

### 2.1 `popularity` / `-popularity` join the show sort whitelist

Add both directions to `schemas.ALLOWED_SORT_KEYS` and to
`browse_queries._SORT_EXPRS`:

```python
"popularity": m.Show.popularity.asc().nulls_last(),
"-popularity": m.Show.popularity.desc().nulls_last(),
```

Both directions, even though the SPA will only ever send `-popularity`,
because every existing key is registered as a pair and a lone key would read
as an oversight. `nulls_last()` on both, matching `premiered` and
`last_aired`: coverage is total today, but the column is nullable and a show
the export has never scored must sort after every scored one, not before.

The tiebreak is `list_shows`'s universal `m.Show.id.asc()`, unchanged. Ties
are real (56k distinct scores over 230k rows, dense at the low end) and `id`
is what the index below is built to break them on; a name tiebreak was
considered and rejected because it would make the ORDER BY stop matching the
index order for no visible benefit at the tail of a search.

The route contract otherwise does not move: `sort` still defaults to `name`
on the API (the *SPA* changes its default, not the server — the API default
is a contract other callers and the tests lean on), an unknown key still 422s,
the eight-key list becomes ten.

### 2.2 A partial index, on NEU-1502's pattern

Add to `m.Show.__table_args__`:

```python
Index(
    "ix_show_popularity_live",
    text("popularity DESC NULLS LAST"),
    "id",
    postgresql_where=text("deleted_upstream_at IS NULL"),
)
```

Same shape, same partial predicate and same comment rationale as
`ix_show_last_aired_live`, so `ORDER BY popularity DESC NULLS LAST, id ASC`
over `WHERE deleted_upstream_at IS NULL` is an index walk.

Measured on the workspace catalog, `-popularity`, count + page shapes
(`EXPLAIN (ANALYZE)`, 50-row limit):

| Request | Without index | With index |
| -- | -- | -- |
| `office` (155 matches) | 10 ms | 2 ms |
| `the` (38k matches) | 528 ms | 389 ms |
| no search, whole catalog | 65 ms | 0.05 ms |

The big-match case is bounded by the hash semi-join whatever the sort — `the`
sorted by `-last_aired` is 414 ms with its index — so the index buys the
unsearched route, not the searched one. It is cheap (190 ms to build, 231k
entries) and its maintenance cost is near zero because the nightly refresh's
`IS DISTINCT FROM` clause only rewrites rows whose score moved. The SPA never
calls `/shows` without a search today, but the route accepts it, and leaving
one sort key as the only one that sequential-scans the table is the kind of
trap NEU-1502 just dug out.

**Migration.** One Alembic revision on the current head (`212671cf449f` at
the time of writing — confirm with `alembic heads` before generating),
`task makemigration -- "add show popularity index"`, then edited down to the
index create and drop. No column, no backfill. Tests build from `create_all`,
so the model's `Index` is what the test schema gets.

### 2.3 People search orders by popularity, then name, then id

`browse_queries.search_people` changes its ORDER BY from
`lower(name), id` to:

```python
.order_by(
    m.Person.popularity.desc().nulls_last(),
    func.lower(m.Person.name).asc(),
    m.Person.id.asc(),
)
```

No `?sort=` parameter on `/people`, no sort control in the SPA: the endpoint
is search-only by design (its docstring explains why there is no browse-all
surface), the overlay treats people as a secondary axis with no controls, and
a sort parameter nobody sends is contract surface for free. Name stays as the
second key so that equal scores — common at 0.6 and below — keep today's
stable, readable order within a band.

**No index on `person.popularity`.** Every people query is a bitmap scan of
`ix_person_name_folded_trgm` followed by a top-N heapsort over the matches;
the sort key does not change the plan. Measured: `john` (11,399 matches) is
52 ms by name and 89 ms by popularity, both dominated by the heap fetches.
Nothing to index.

### 2.4 The query-count pins are unaffected

`test_get_shows_issues_a_fixed_number_of_queries_whatever_the_page_size` (4)
and its searched twin (6) must stay at their numbers: this ticket adds no
query to either path. Run them; do not touch them.

## 3. Acceptance criteria

Functional (integration tests, seeded catalog, `tvbf_test`):

- `GET /shows?sort=popularity` and `?sort=-popularity` are accepted; every
  other string still 422s (`test_get_shows_sort_invalid_returns_422` stays
  green, and a positive test for each new key joins it).
- In `tests/integration/catalog/test_browse_queries.py`, a
  `test_list_shows_sort_popularity_desc` on the model of
  `test_list_shows_sort_last_aired_desc`: three seeded shows with scores
  (say 150.0, 11.3, `NULL`) come back highest first, the null last; the
  ascending key reverses the scored ones and still puts the null last.
- Two shows with the same score come back in `id` order under both keys.
- `-popularity` combined with `search=` returns the matched set in score
  order (one test; the predicate and the sort are independent, this pins
  that nobody made the sort conditional on the search).
- `search_people`: `test_search_people_sorted_by_name` (line ~81) and the
  pagination test (~96) are rewritten, not deleted — give the fixtures
  distinct scores and assert score order, then add a same-score case that
  asserts the name order within the band. The suite's remaining assertions
  are single-row and unaffected.
- The §2.4 pins, unchanged.

Performance (workspace database, `EXPLAIN (ANALYZE)`, recorded in the PR
description per NEU-1502's convention — no benchmark harness):

| Request | Target |
| -- | -- |
| no search, page, `-popularity` | ≤ 5 ms, plan is an `Index Only Scan` on `ix_show_popularity_live` |
| `office`, count + page, `-popularity` | ≤ 50 ms |
| `the`, page, `-popularity` | ≤ 500 ms (same bound NEU-1502 set for `-last_aired`) |
| `carell`, people page | ≤ 50 ms |

## 4. Docs this changes

- `.claude/CLAUDE.md` / `AGENTS.md` **Browse subsystem**: the `sort` bullet
  (line ~275) becomes *ten* whitelist keys and names `popularity` and its
  index; the `/people` description gains the order. `README.md`'s `GET
  /shows` row (~88) if it enumerates sort keys.
- `.claude/docs/architecture-database.md`: `ix_show_popularity_live` beside
  `ix_show_last_aired_live`, with the note that no column was added.
- `.claude/docs/architecture-endpoints.md`: `/people` is popularity-ordered.
- `CONTEXT.md`: already updated (**Popularity**). Use the term and respect
  its *avoid* list in comments and docstrings — not "most popular", not
  "trending".
- No ADR: an index and an ORDER BY are cheap to reverse, and the glossary
  plus this spec carry the why.

## 5. Out of scope

- Any change to the API's default sort (`name`); the SPA default is the
  frontend half's decision.
- Exposing `popularity` on `ShowOut` / `PersonOut`, or any UI that shows the
  number. The sort is server-side and the SPA needs nothing new in the
  payload.
- A nightly refresh of `person.popularity` from TMDB's person export, or any
  other fix for its mixed vintage.
- Relevance ranking, query-aware boosting, blending popularity with match
  quality — the "no new search infrastructure" posture from NEU-433 and
  NEU-1502 stands. This is one ORDER BY.
- The `the`-shaped search cost (~400 ms under any sort): that is the semi-join,
  not this ticket.
