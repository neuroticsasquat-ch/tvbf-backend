# NEU-1175 — Suppress recommendations for shows the viewer already has a record for

**Ticket:** [NEU-1175](https://linear.app/neuroticsasquatch/issue/NEU-1175/backend-suppress-recommendations-for-shows-now-in-my-shows)
**Parent story:** [NEU-1174](https://linear.app/neuroticsasquatch/issue/NEU-1174/a-show-i-add-stops-appearing-in-this-weeks-recommendations)
**Repo:** `tvbf-backend`
**Project:** TVBF: Maintenance
**Project spec:** `docs/specs/tvbf-personalized-recommendations-project-spec.md` (umbrella) §8, §9, §11
**Contract doc:** `tvbf-backend/docs/specs/NEU-1112-recommendations-page-contract.md` (cited by `tvbf-frontend`)
**Blocks:** NEU-1176 (frontend refresh after an add), NEU-1178 (dismissal, which adds a fifth source to this definition)
**Status:** approved for implementation

This spec lives in the umbrella `docs/` because it is not itself cited across
repos. The cross-repo half of the change is an edit to the NEU-1112 contract
doc, which already lives in-repo for that reason (CLAUDE.md's
cross-repo-citation rule).

---

## 1. What is wrong

`app.user_recommendation_set` is an immutable snapshot and the read path filters
only `adult` and `deleted_upstream_at`. A show the user acts on after the set was
generated keeps occupying one of the twelve cards until Sunday's pass supersedes
the whole set — up to seven days.

Observed on production 2026-08-17: four of the five shows on the page had been
added to My Shows and all four were still displayed.

## 2. What to build

`GET /me/recommendations` stops serving a stored suggestion once the viewer has a
record for that show, and the next stored suggestion takes its place.

The set is never mutated. Suppression is a property of the reader's current
state, not of the generated set, so it belongs in the read query and nowhere
else.

## 3. The rule

**A show the viewer has a record for is not served**, where "has a record" is
project spec §8's own phrase and its own four sources:

| Source | Table | Show id |
| -- | -- | -- |
| My Shows membership | `app.user_show_watch` | column |
| A show rating | `app.user_show_rating` | column |
| Any episode watch | `app.user_episode_watch` | via `catalog.episode.show_id` |
| Any episode rating | `app.user_episode_rating` | via `catalog.episode.show_id` |

`catalog.episode.show_id` is `NOT NULL`, and all four columns are foreign-keyed
to `catalog.show`, so neither the SQL form of this set nor the Python one can
name a show the other cannot.

### 3.1 Why the full union rather than My Shows alone (D1)

The parent story scoped this to My Shows membership — "the user acting on a
recommendation". That was widened deliberately, because the narrow rule leaves a
reachable hole: `show_membership_repo.add` is called from exactly one place
(`my_shows_service.add`), so marking an episode watched, rating an episode, or
rating the show **never** creates a My Shows row. A user who opens a recommended
show from the grid, rates it and marks two episodes — all reachable from the show
detail page today — would keep that card for the rest of the week.

The wider rule also makes the read filter and the write filter the *same
sentence*: §8 already says never recommend a show the user has a record for, and
after this ticket that is enforced at both ends rather than only at generation.
The cost is three more sources in one subquery; the query count does not change.

### 3.2 What the rule is not

* It is **not** a rating, a dismissal or an opinion. It is a statement that the
  user has already met the show. NEU-1178's dismissal is a *fifth* source of the
  same exclusion and must reach it without passing through `taste_for_user` — see
  that ticket's neutrality argument.
* It **never mutates or deletes** the set's rows. Immutability is what makes the
  weekly swap atomic and non-destructive (§9), and what keeps `raw_response` and
  the stored rows usable as the record of what the model actually said — which is
  how the two failures of 2026-08-17 were diagnosed at all.
* It **never renumbers `rank`.** Rank is the model's own ordering and the
  contract says a client displays the value, not its index, so ranks stay
  non-contiguous exactly as the read-time `adult` filter already leaves them.

## 4. Where it lives

### 4.1 `src/tvbf/recommendations/exclusion.py` — new, the one definition (D2, D3)

The rule is expressed **once**. Today it exists in Python inside
`payload.build_payload`; a second expression in SQL is exactly the failure mode
`recommendation_repo`'s own docstring exists to prevent, one layer up.

```python
def show_ids_with_a_record(user_id: UUID) -> Select[tuple[int]]:
    """Every show this user has a record for (project spec §8), as a select."""

async def load_show_ids_with_a_record(db: AsyncSession, *, user_id: UUID) -> frozenset[int]:
    """The same answer, materialised, for the payload builder."""
```

* Built from `union_all` of the four branches, wrapped in a subquery so the
  return type is a plain `Select` usable both as an `IN` operand and by
  `db.scalars`. `union_all` rather than `union`: dedup buys nothing for a
  membership test or a `frozenset` and costs a hash aggregate.
* Imports **models only**. `recommendations/` already imports `app.repos`
  (`taste`, `payload`, `completion`), so a repo importing this module makes the
  package edge two-way — acyclic at module level, and only because this module
  imports no repo. Keep it that way.
* It lives beside `taste.py` / `payload.py` / `resolution.py` because §8 is where
  the rule is written down and `payload.py` is one of its two callers. The
  precedent for a rule module imported by repos is `catalog/episodes.py`
  (`IS_SPECIAL`, `EPISODE_ORDER`) and `catalog/seasons.py`.

### 4.2 `app/repos/recommendation_repo.list_current_recommendations` — the anti-join

One clause added to the existing statement:

```python
UserRecommendation.show_id.not_in(exclusion.show_ids_with_a_record(user_id)),
```

Still one statement, still no limit, still ordered by `rank`. The docstring gains
the reason: what this function answers is "the current set's suggestions **this
reader has not already met**", and that is part of what a reader's current set
*is* — which is why it goes here rather than in the service, where the weekly
pass and the API could come to disagree about it.

The weekly pass is unaffected: it reads `get_current_set` for the hash, and this
function's only `src/` caller is the API service.

### 4.3 `recommendations/payload.build_payload` — same set, one source

```python
signals = await taste_for_user(db, user_id=user_id, now=now_dt)
excluded = await exclusion.load_show_ids_with_a_record(db, user_id=user_id)
```

replacing the `episode_rating_repo.mean_stars_per_show_for_user` call and the
Python union. Drop that import if nothing else in the module uses it
(`taste_for_user` makes its own call for stars, which stays).

**This is a swap, not an addition** — the pass's query count is unchanged.

It also removes a live coupling: today `excluded` is correct only because
`taste_for_user`'s universe happens to be exactly *my_shows ∪ episode-watched ∪
show-rated*. Narrow that universe for a taste reason some day and the
never-recommend set silently narrows with it — a change to what we *say about* a
user quietly changing what we *never show* them.

### 4.4 `app/services/recommendation_service` — one docstring, no behaviour

`DISPLAY_LIMIT = 12` off the front of the rank-ordered rows is unchanged; the
slice is what promotes the next suggestion for free.

`hydrate_my_ratings` **stays uncalled and `my_rating` stays null**, but the
docstring's reasoning is now stronger rather than weaker: a show the user has
rated is a show they have a record for, so it is excluded at generation *and*
suppressed at read time. A non-null value would mean the rule had failed at both
ends, and the round trip would still display nothing.

### 4.5 No `PROMPT_VERSION` bump (D4)

CLAUDE.md's rule covers the prompt text and the payload's shape, widened by
NEU-1173 to the whole request/response contract. This is neither: same
instruction, same groups, same columns, same row order, and — per §5's equality
test — the same `exclude` contents. A bump would regenerate every account next
Sunday to produce byte-identical payloads.

If some account does turn out to differ, its hash changes and it regenerates on
its own next Sunday. Self-healing, and the right size of response.

## 5. Acceptance criteria

1. A set with 15 stored rows where the user has added rows 1–3 to My Shows
   returns rows 4–15 — twelve cards, in rank order, ranks unchanged.
2. A set with 13 stored rows where the user has a record for five returns eight
   cards, `200`, no error. Fewer than twelve is a normal answer: no backfill from
   an older set, no error, no empty state.
3. A set where the user has a record for every show returns
   `200 {"recommendations": []}` — the same body as no set at all, never a `204`
   and never a `500`.
4. **(a)** A show suppressed only by My Shows membership reappears when that
   membership is removed — suppression is a live join, not a stored flag.
   **(b)** A show the user also has an episode watch or a rating for stays
   suppressed after that removal, because the record that suppresses it is still
   there. Un-adding a show you watched three episodes of does not unmake those
   episodes, and the next run would exclude it at generation time regardless.
5. Each of the four sources suppresses on its own: My Shows, a show rating, an
   episode watch, an episode rating.
6. The read costs **three queries** — the rows (anti-join included) and
   `hydrate_show_refs`' pair — whatever the size of the set and however many rows
   are suppressed. One query when nothing survives, since `hydrate_show_refs`
   short-circuits on an empty list. Not one query per row.
7. `build_payload`'s query count is unchanged, and its `excluded_show_ids` is
   identical to the Python union it replaces.
8. Another user's records never suppress anything here, and never affect another
   user's payload.
9. The set's rows are never mutated or deleted, and `rank` values are the stored
   ones.

## 6. Tests

| Where | What |
| -- | -- |
| `tests/integration/recommendations/test_exclusion.py` (new) | Each of the four sources on its own; a user with all four; scoping to one user; a show with no record absent from the set |
| `tests/integration/app/repos/test_recommendation_repo.py` | Suppression per source; ranks unchanged and non-contiguous; the rows still in the set afterwards (AC 9); AC 4a/4b |
| `tests/integration/routers/test_me_recommendations.py` | AC 1, 2, 3; the query-count test on `test_trending.py`'s `before_cursor_execute` pattern (AC 6) |
| `tests/integration/recommendations/test_payload.py` | AC 7 — seed a user with all four kinds of record and assert `excluded_show_ids == frozenset(signals) \| frozenset(episode_stars)`, the expression being replaced. This is what backs "no `PROMPT_VERSION` bump" |

## 7. Documentation

**`tvbf-backend/docs/specs/NEU-1112-recommendations-page-contract.md`** — the
frontend cites this by URL and must not re-implement the rule. §4 ("The cap and
the filters are the server's") gains the suppression: the list is *the top twelve
stored suggestions the viewer has no record for*, with the four sources named so
NEU-1176's author knows which actions warrant a refetch, and a note that ranks
stay non-contiguous and that fewer than twelve is normal. §5's "no dismissal"
line stays as it is — that is NEU-1178's to change.

**`tvbf-backend/.claude/CLAUDE.md`** (D6) — `exclusion.py` in the
`recommendations/` module map, and a non-obvious-pattern entry recording that the
never-recommend rule is enforced **twice**, at write time and at read time, from
one definition, so a fifth source added to `exclusion.py` changes both. No ADR:
this reverses nothing, it enforces §8 in a second place.

## 8. Out of scope

* **Dismissal** — NEU-1178. It adds a fifth source to `exclusion.py` and reuses
  this suppression seam; nothing here should anticipate it beyond leaving the
  definition in one place.
* **Refreshing the grid after an add** — NEU-1176, in `tvbf-frontend`.
* **Regenerating the set when it shrinks.** Adding a show changes the taste
  payload, so the hash changes and that user's next Sunday run is not skipped.
  Suppression only has to hold for the rest of the week.
* **Serving `my_rating`** — still null, still no round trip (§4.4).
* **Backfilling from an older set** to keep the grid at twelve. The 25-asked-for
  headroom is the mechanism; when it runs out, fewer cards is the answer.
