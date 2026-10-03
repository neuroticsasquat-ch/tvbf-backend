# NEU-1502 — Speed up show search (backend half)

**Ticket:** [NEU-1502](https://linear.app/neuroticsasquatch/issue/NEU-1502/speed-up-show-search)
**Repo:** `tvbf-backend` — branch `tom/neu-1502-speed-up-show-search` from `main`
**Project:** tvbf: Maintenance
**Other half:** `tvbf-frontend/docs/specs/NEU-1502-search-running-indicator.md` (the spinner and keep-previous-results; independent of this half, same ticket)
**Precedents this consumes:** `docs/superpowers/specs/2026-05-03-show-akas-search-design.md` (AKA search), `docs/superpowers/specs/2026-06-29-neu-433-search-normalization-design.md` (the fold, and the "defer indexing until measured" clause this spec is the measurement for), `src/tvbf/sql_fold.py` (the one fold), `docs/specs/NEU-1005-tombstone-shows-deleted-upstream.md` / ADR-0005 (tombstones hidden from discovery), NEU-1062 and `tests/integration/app/repos/test_specials_ledger.py` (specials are excluded from aired math)
**Glossary:** `CONTEXT.md` — **Last aired**, **Short query** (both added by this ticket's design session)
**Status:** approved for implementation

---

## 1. The problem, measured

Search has slowed as features accreted. Measured on the workspace database
(Postgres 18.6, 231,538 shows, 161,075 AKAs, the full catalog), one
`GET /shows?search=office&sort=-last_aired&per_page=50` runs two catalog
queries that each scan every show:

| Query | Time |
| -- | -- |
| COUNT, current predicate | 1,124 ms |
| page, current predicate, sorted by last aired | 1,304 ms |
| the same two, predicate rewritten as §2.1 | 35 ms total |
| `the office`, rewritten as §2.1 | 4 ms |
| any query whose tokens are all 1–2 characters, either predicate | 1.5–1.9 s |
| `the` alone, rewritten predicate, sorted by last aired | 5.9 s |
| plain browse, no search, sorted by last aired | 4.7 s |

Two independent causes, and this ticket fixes both.

**The predicate defeats the index.** NEU-433's `list_shows` builds, per token,
`folded(show.name) LIKE needle OR show.id IN (select show_id from show_aka where
folded(title) LIKE needle)`. The trigram expression indexes on both folded
columns exist (`ix_show_name_folded_trgm`, `ix_show_aka_title_folded_trgm`,
migration `aa4571de8f17`), and the AKA half *does* use its index. But Postgres
cannot drive `ix_show_name_folded_trgm` through that `OR`, so it folds all
231k names for every token, twice per request (count + page). Row estimates
are also off by three orders of magnitude (115,266 estimated vs 155 actual),
because the needle is an expression rather than a constant.

**The last-aired sort is computed per row.** `_LAST_AIRED` is a correlated
`max(episode.air_date) … <= current_date` subquery evaluated for every
candidate row before `LIMIT`. It is invisible on a 155-row match and
catastrophic on a 48k-row one (`the`) or on unfiltered browse.

Everything else on the route is already fixed-cost: two hydration IN-queries,
two AKA-badge IN-queries, ratings and membership by PK, no lazy loads, no
per-row follow-ups. The people search that fires beside it is 2.5 ms. **This
spec does not touch those**, nor the per-request session touch/commit, nor
`select(m.Show)`'s column list.

## 2. Decisions

### 2.1 The predicate: one semi-join per source, all tokens in it

Replace the per-token `OR` with one membership test:

```sql
show.id IN (
  SELECT id      FROM catalog.show     WHERE folded(name)  LIKE %t1% AND folded(name)  LIKE %t2% …
  UNION
  SELECT show_id FROM catalog.show_aka WHERE folded(title) LIKE %t1% AND folded(title) LIKE %t2% …
)
```

Both halves become bitmap index scans (a bitmap AND across tokens within a
half), and the outer query is a hash semi-join on `show_pkey`. The COUNT query
is built from the same `base`, as today, so it inherits the shape.

**This tightens the semantics, on purpose.** Today a show matches when *each*
token is found in the name *or* in some AKA, independently — so `the office`
also returns *Phantom Lawyer*, whose `office` comes from the AKA *Shin I-rang
Law Office* and whose `the` comes from a different title of the same show.
After this change a match must live entirely in the name or entirely in one
AKA. On the workspace catalog this drops 7 of the 56
`the office` results, every one of them spurious. It is also what
`hydrate_matched_aka` has assumed since NEU-433 — it looks for one AKA that
matches *every* token, or a name that does — so those 7 shows currently come
back with no badge and no visible reason. The rewrite makes the list and the
badge agree.

**Keep the two rules from being able to drift.** `list_shows` and
`hydrate_matched_aka` must build their per-column predicates through one
module-level helper (`_title_predicate(column, tokens)` or similar) that
returns the `AND` of the folded `LIKE`s for a column, and applies the §2.2
short-query rule. NEU-433 learned this the hard way when the badge and the
list folded separately; the same helper is the fix here. Do not use
`LIKE ALL (ARRAY[…])`: measured, it falls back to a sequential scan (1.5 s)
because pg_trgm's index support does not extend to the array form.

The tokenizer is unchanged: split on whitespace, drop tokens that fold to
nothing, `WHERE false` when none survive (NEU-433 §"Empty-token guard").

### 2.2 Short queries match the start of a title

pg_trgm needs one complete trigram to use the index for a substring, so a
query whose folded tokens are *all* shorter than three characters (`v`, `24`,
`er`, `24 h`) can only be a full scan under `%…%` — 1.5–1.9 s, on every
keystroke that pauses at one or two characters. A frontend minimum length was
rejected: 1,441 live shows have folded titles of one or two characters and
would become unfindable.

**Rule.** A **short query** (glossary) is one none of whose surviving tokens has
a folded length ≥ 3. A short query matches when the *whole folded query*
(tokens concatenated in order, which is what the fold does to a title's spaces
anyway) is a **prefix** of the folded name or of a folded AKA title:

```sql
folded(name) LIKE folded('24 h') || '%'     -- '24h%' — finds "24 H", not "Room 24"
```

pg_trgm pads the start of an indexed string, so a 1- or 2-character prefix is
indexable: measured 17 ms for `v%` and 2 ms for `er%`. Any query with at least
one token of three or more folded characters is an ordinary §2.1 substring
search; its short tokens ride along as extra `LIKE`s, which the bitmap AND
applies as rechecks (measured 1.5 ms for `office` + `us`).

The rule applies identically to the name half and the AKA half, and — via the
shared helper — to `hydrate_matched_aka`, so a short query's badge decision
uses the same prefix test.

### 2.3 Wildcards need no escaping — pin it

The design session asked whether `%` and `_` in a token should be escaped, as
handle search does (NEU-1163). **They do not need to be**: both are POSIX
`[[:punct:]]`, and `folded()` strips punctuation before the pattern is built.
`a%b_c%` folds to `abc`; a bare `%` folds to nothing and is dropped by the
empty-token guard. Add one integration test asserting exactly that (`a%b`
matches *ABC*-titled shows only as the literal `ab`, and `%` alone returns no
rows), so nobody "fixes" it into a double escape later.

### 2.4 `catalog.show.last_aired`, a stored derived date

Add `last_aired DATE NULL` to `catalog.show`. Definition (glossary **Last
aired**): the maximum `episode.air_date` over the show's **regular** episodes
(`NOT IS_SPECIAL` from `catalog/episodes.py`) with `air_date <= today`; `NULL`
when there is none. `today` is `datetime.now(UTC).date()`, passed in as a bound
parameter — the jobs' convention (`tmdb/update.py`, `tmdb/ingest.py`,
`push/delivery.py`), not `func.current_date()`, whose answer depends on the
connection's timezone setting.

This is a column on the spine, not a sidecar, on the precedent of
`show.runtime` — a derived per-show value that `refresh_runtime` recomputes
after every series upsert — and of the generated `is_ended`. It is derived
from spine data only (episode air dates, which already carry the offset
correction on the way in), so CONTEXT.md's "never TV Maze-derived on the
spine" rule is not engaged.

**Two visible consequences, both accepted in the design session.** Browse's
last-aired order now excludes specials, where `_LAST_AIRED` counted them; a
show whose only recent activity is a retrospective no longer floats to the
top. And it rolls forward on the server's UTC day, so it is the catalog-wide
surfaces' "today", not a viewer's. **The My Shows, Watch Next and Watched
surfaces keep their live `episode_repo.latest_aired_per_show`** with the
client-supplied `?today=`: those queries run over a viewer's few dozen shows
and are cheap, and a US viewer must not see tomorrow's episode as aired at
19:00 local. The two agree on the specials rule, so the *definition* is shared
even though the read path is not.

**Index.** `ix_show_last_aired_live` on `(last_aired DESC NULLS LAST, id)
WHERE deleted_upstream_at IS NULL`, matching `ix_show_first_air_date_live`'s
partial-index pattern and the `ORDER BY …, id` tiebreak `list_shows` already
uses, so unfiltered browse by last aired becomes an index walk.

**One recompute, four callers.** A new module `src/tvbf/catalog/last_aired.py`
owns `async def recompute_last_aired(session, *, today: date, show_ids:
Sequence[int] | None = None) -> int` — one `UPDATE catalog.show SET last_aired
= agg.d FROM (SELECT show_id, max(air_date) …) …` plus a second statement (or a
`LEFT JOIN`) that nulls shows with no qualifying episode, scoped to `show_ids`
when given and to the whole catalog when `None`. It must reuse `IS_SPECIAL`,
not restate it. Callers:

1. `tmdb/upsert.py::upsert_series_payload`, immediately after
   `refresh_runtime(session, show_id=show_id)` (line ~1841) — the one path
   through which the full pass and the daily delta write episodes. Same
   transaction as the upsert, so `mirror_series`'s per-show commit covers it.
2. `catalog/offsets.py::project_offsets`, at its end — the only other writer
   of `episode.air_date`. Same transaction as `mark_reconciled`.
3. `tmdb/update.py::run_catalog_update`, after `reconcile_against_export` and
   before `finalize_run`, with `show_ids=None`: the **daily roll-forward**.
   Episodes cross `air_date <= today` without any row changing, so this is
   what keeps yesterday's premiere sorted correctly. Measured at ~1.0 s over
   6.6M episodes (parallel seq scan + hash aggregate). Run it in its own
   `_owned_session`, catch and log its failure the way
   `reconcile_against_export` swallows its own — a failed roll-forward must
   not fail the delta, and the next day's run heals it (the recompute is
   idempotent and total).
4. The migration's backfill (below) — which cannot import this module, because
   no migration in `migrations/versions/` imports application code, so the
   backfill is a deliberate one-shot inline copy of the same SQL with a comment
   pointing at the module.

The one-off passes (`orphan_retire`, `episode_repoint`, `season_dedupe`,
`episode_map`) have already run in production and are not hooked; the daily
roll-forward covers any future re-run within a day.

**Migration.** One Alembic revision on head `d2a8f6c41e07`, generated with
`task makemigration -- "add show.last_aired"` and then edited: add the column
nullable; backfill with the inline aggregate `UPDATE` (231k rows; the aggregate
alone is ~1 s here); create the partial index. Downgrade drops the index and
the column. Tests build the schema from `create_all`, so the model gains the
column and the index definition too (`models.py`'s note on this).

**Sort.** `_SORT_EXPRS["last_aired"]` / `["-last_aired"]` become the column
(`asc().nulls_last()` / `desc().nulls_last()`); delete `_LAST_AIRED`. The
`last_aired` sort key's name, its position in `ALLOWED_SORT_KEYS`, and the
route contract are unchanged.

`show.last_air_date` (TMDB's own frozen field) is **not** a substitute: on a
sample of 2,000 recently synced shows it disagrees with the computed value on
625 and is null on 190. It stays what it is — the `ended` date on the summary.

### 2.5 The query-count pin grows a search case

`test_get_shows_issues_a_fixed_number_of_queries_whatever_the_page_size`
pins 4 catalog queries for an unsearched request. Add the searched variant
pinning **6** (count, page, genres, networks, AKA badge ×2), so a future
per-row follow-up on the search path trips a test rather than a user.

## 3. Acceptance criteria

Functional (integration tests, seeded catalog, `tvbf_test`):

- All existing search suites stay green unchanged in intent:
  `test_browse_aka_search.py`, `test_search_normalization.py` (`shogun`,
  `spiderman`, `alien earth`, `the office us`, `進撃`, `--`),
  `test_person_search.py::…show search unaffected…`, the tombstone cases in
  `test_browse_queries.py`, and `test_browse.py`'s search routes. One test may
  need its fixture adjusted if it relied on a mixed name/AKA match; if so,
  say so in the PR.
- A show whose name carries one token and whose AKA carries the other is **not**
  returned for the two-token query (the §2.1 tightening), and
  `hydrate_matched_aka` reports the matched AKA for every AKA-only result the
  list returns (the two rules agree).
- Short queries: `er` returns *ER* and not *Cheers*; `24 h` returns *24 H*;
  `v` returns *V*; `office us` (one long token) still returns *The Office (US)*.
- `a%b` and `%` behave as §2.3.
- `recompute_last_aired`: ignores a later special, ignores a future-dated
  regular episode, nulls a show with none, scopes to `show_ids` when given.
- `upsert_series_payload` and `project_offsets` leave `last_aired` correct for
  the show they touched (extend `test_upsert_airdates.py` and
  `test_airdate_projection.py`).
- `run_catalog_update` rolls forward: a show whose regular episode aired
  "today" relative to the run's clock has `last_aired` set after the run; a
  failing recompute is logged and the run still finalizes as succeeded.
- `test_list_shows_sort_last_aired_desc` passes with the fixture calling
  `recompute_last_aired` after seeding (it seeds episodes directly today), and
  its expected order is unchanged (`[88002, 88001, 88003]`).
- The specials ledger: add a row for `catalog.last_aired.recompute_last_aired`
  as `EXCLUDE_BOTH` if the tripwire enumerates this module; the
  ignores-a-later-special test is required either way.
- The §2.5 pin: 6 catalog queries for a searched page, whatever `per_page`.

Performance (measured on the workspace database with `EXPLAIN (ANALYZE)`, and
recorded in the PR description — there is no benchmark harness and none is
being added; the test database is too small for the planner to choose these
plans, so an EXPLAIN-based test would be a false pin):

| Request | Target |
| -- | -- |
| `office`, count + page, `-last_aired` | ≤ 50 ms |
| `the office`, count + page | ≤ 50 ms |
| `er` (short query), count + page | ≤ 50 ms |
| `the` alone, page, `-last_aired` | ≤ 500 ms |
| no search, page, `-last_aired` | ≤ 50 ms |
| the daily roll-forward | ≤ 5 s |

Both search plans must show bitmap index scans on `ix_show_name_folded_trgm`
and `ix_show_aka_title_folded_trgm` and no `Seq Scan on show` outside the
hash-join build side.

## 4. Docs this changes

- `.claude/CLAUDE.md` / `AGENTS.md` **Browse subsystem**: the `search` bullet
  now says *all tokens in the folded name, or all tokens in one folded AKA*,
  states the short-query prefix rule, and names the shared predicate helper;
  the `sort` bullet notes `last_aired` reads the stored column; a line under
  the module map for `catalog/last_aired.py`; the delta description gains the
  roll-forward. `README.md` §browse (lines ~88–96) to match.
- `.claude/docs/architecture-database.md`: `show.last_aired` and its index,
  with the four maintainers.
- `CONTEXT.md`: already updated (**Last aired**, **Short query**).
- No ADR: a stored derived column is cheap to reverse, and the spec plus the
  glossary carry the why.

## 5. Out of scope

- Relevance ranking, prefix-boosting, a tsvector column or a search engine —
  the repo's "no new search infrastructure" posture stands; this ticket is the
  measurement NEU-433 deferred to and the index it already built.
- Switching the My Shows surfaces to the stored column (§2.4).
- People search, the per-request session touch and commit, the summary's
  column list, and cancelling the database query when the browser aborts a
  superseded request (moot once a search costs milliseconds).
- The frontend indicator and keep-previous-results — the other half of this
  ticket, in the frontend repo.
