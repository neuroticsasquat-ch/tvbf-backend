# NEU-1184 — The library mark on `GET /shows` and `GET /shows/{id}/similar`

**Story:** [NEU-1184](https://linear.app/neuroticsasquatch/issue/NEU-1184/mark-shows-already-in-my-shows-on-every-browse-surface)
**Children:** [NEU-1185](https://linear.app/neuroticsasquatch/issue/NEU-1185/backend-serve-in-my-shows-from-get-shows-and-showsidsimilar) (backend) · [NEU-1186](https://linear.app/neuroticsasquatch/issue/NEU-1186/frontend-mark-tracked-shows-on-search-results-and-the-similar-tab) (frontend)
**Repos:** `tvbf-backend`, `tvbf-frontend` — both branch from `main`
**Project:** TVBF: Open Registration · Milestone 0, UI consistency
**Related:** NEU-1053 (`/shows/{id}/similar` as first built), NEU-1056 / NEU-1059 (the two surfaces that already carry the mark), NEU-1057 (one picture for one claim), NEU-1183 (where the mark sits on a card), NEU-1187 (the add/remove control), NEU-1188 (grid/list parity)
**Status:** approved for implementation

This spec lives in-repo rather than in the umbrella `docs/` because it is a
**cross-repo contract**: `tvbf-frontend`'s NEU-1186 cites the response shape and
the consumption rules below by URL, and the umbrella `docs/` is not a git repo,
so nothing in it can be linked or diffed (CLAUDE.md's cross-repo-citation rule).
It is the whole served contract of both routes, not only the delta — a
delta-only document would leave the consuming ticket reading source to find out
what `genres` contains.

---

## 1. The problem, and the part of it that is not the problem

Search results render the viewer's own rating and no library mark, so the app
demonstrably knows the show is tracked and declines to say so — on the one
surface where *"should I add this?"* is the actual question.

Reproduced on production build `6669017` at 375×812: search `the bear` → grid →
*The Bear* shows **★4.5** (the viewer's rating) and **★4.1** (the TMDB average)
and **no library mark**, while sitting in My Shows.

**The cause is a backend gap, not placement.** `ShowCard` renders the mark
whenever `in_my_shows` arrives, and `ShowGrid` already widens its `shows` prop to
`ShowSummary & { in_my_shows?: boolean }` and threads it. Two of the four
endpoints feeding that grid never send the field:

| Endpoint | `in_my_shows` before | `my_rating` before |
| -- | -- | -- |
| `GET /trending` | ✅ | ✅ |
| `GET /anticipated` | ✅ | null by contract |
| `GET /shows` (browse + search) | ❌ | ✅ |
| `GET /shows/{id}/similar` | ❌ | ❌ |

## 2. Wire shape

### 2.1 The one place the field is declared

`in_my_shows` is declared **once**, on a new intermediate:

```python
class MarkedShowOut(ShowSummary):
    in_my_shows: bool          # required — no default


class TrendingShowOut(MarkedShowOut): ...      # GET /trending
class AnticipatedShowOut(MarkedShowOut): ...   # GET /anticipated
class BrowseShowOut(MarkedShowOut): ...        # GET /shows items
class SimilarShowOut(MarkedShowOut): ...       # GET /shows/{id}/similar
```

Three decisions are folded into that, and each is easy to undo by accident.

**It is not hoisted onto `ShowSummary`.** That type is nested inside six `/me`
payloads (`MyShowEntry.show`, watch-next, upcoming, …, built by
`my_shows_service.build_show_summary_from_refs`) and is the base of `ShowDetail`.
A field on the base would emit `in_my_shows: false` on every My Shows row —
where the truth is *always* `true` — and add an uncomputed field to the detail
payload, which learns membership a different way: `ShowDetailPage` derives it
from the whole `useMyShows()` list client-side, and needs nothing from
`GET /shows/{id}`.

**It is one intermediate rather than four copies.** `TrendingShowOut` and
`AnticipatedShowOut` each declared the boolean separately, with docstrings
arguing they are *siblings, not a shared type*. That reasoning was about
everything *around* the field — different bodies, different `my_rating` rules —
and it survives: each surface keeps its own subclass and its own docstring. What
does not survive four occurrences is the field itself. The subclasses are
re-parented; **their served shape does not change**, which is what keeps
NEU-1185's "existing behaviour of `/trending` and `/anticipated` is untouched"
literally true.

**It is required, with no default.** All four construction sites pass it
explicitly today. A fifth surface that forgets should fail at type-check rather
than serve `false` for a show the viewer tracks — that silent-false failure is
the same one that ruled out hoisting to `ShowSummary`, and a `= False` default
re-admits it one level down. `RecommendationOut` stays a direct subclass of
`ShowSummary` and gains nothing; see §6.

### 2.2 `GET /shows`

`200` with `ShowListPage`, `items` now typed `list[BrowseShowOut]`:

```json
{
  "items": [
    {
      "id": 136315,
      "name": "The Bear",
      "type": null,
      "status": "Ended",
      "language": "en",
      "premiered": "2022-06-23",
      "ended": "2025-10-01",
      "image_medium": "https://image.tmdb.org/t/p/w342/abc.jpg",
      "image_original": "https://image.tmdb.org/t/p/original/abc.jpg",
      "network": { "id": 453, "name": "Hulu", "image_medium": null },
      "web_channel": null,
      "genres": ["Comedy", "Drama"],
      "matched_aka": null,
      "rating_average": 8.2,
      "my_rating": 4.5,
      "in_my_shows": true
    }
  ],
  "page": 1,
  "per_page": 50,
  "total": 1,
  "total_pages": 1
}
```

Only `in_my_shows` is new. Query parameters, filter semantics, the eight sort
keys, offset pagination and the 422 on an unknown sort key are all unchanged.

### 2.3 `GET /shows/{show_id}/similar`

`200` with a **bare array** of `SimilarShowOut`:

```json
[
  {
    "id": 60059,
    "name": "Better Call Saul",
    "type": null,
    "status": "Ended",
    "language": "en",
    "premiered": "2015-02-08",
    "ended": "2022-08-15",
    "image_medium": "https://image.tmdb.org/t/p/w342/def.jpg",
    "image_original": "https://image.tmdb.org/t/p/original/def.jpg",
    "network": null,
    "web_channel": null,
    "genres": [],
    "matched_aka": null,
    "rating_average": 8.7,
    "my_rating": 4.0,
    "in_my_shows": true
  }
]
```

`in_my_shows` **and** `my_rating` are new here. `404` for a show id no
`catalog.show` row has; `200 []` for a show with no recommendations — roughly 8%
of the long tail, so an empty result cannot stand in for a missing show. The cap
of 12, TMDB's own rank order, and the read-time `adult` /
`deleted_upstream_at` filters (applied *before* the cap, which is what storing
twenty leaves headroom for) are all unchanged.

### 2.4 Field-by-field, both routes

| Field | `GET /shows` | `GET /shows/{id}/similar` | Why |
| -- | -- | -- | -- |
| `in_my_shows` | **filled** | **filled** | this spec |
| `my_rating` | filled | **filled** | this spec; see §3.2 |
| `rating_average` | filled | filled | on the row |
| `genres` | filled | **always `[]`** | `ShowCard` renders neither, and hydrating them is two more round trips for fields nothing displays |
| `network` | filled | **always `null`** | as above |
| `matched_aka` | filled when a search term matched an AKA rather than the name | always `null` | there is no search term on `/similar` |
| `web_channel` | **always `null`** | **always `null`** | TMDB draws no broadcaster/streamer distinction; `catalog.network` absorbed the concept. The key stays because removing it is a contract change no ticket covers |

`genres` / `network` staying empty on `/similar` is unchanged from NEU-1053 and
is *not* re-decided here. It is the reason a hypothetical list-view consumer of
that route would need them hydrated first — no such consumer exists, and
NEU-1188 does not create one.

## 3. Caching

| Route | Before | After |
| -- | -- | -- |
| `GET /shows` | `private, no-store` | unchanged |
| `GET /shows/{id}/similar` | `private, max-age=300` (router default) | **`private, no-store`** |
| `GET /trending` | `private, no-store` | unchanged |
| `GET /anticipated` | `private, no-store` | unchanged |
| `GET /shows/{id}` | `private, no-store` | unchanged |

### 3.1 `/shows` needs no header change, and the ticket's premise about it was wrong

NEU-1184 was written on the belief that browse routes carry
`public, max-age=300` and that adding the mark would cost `/shows` its
cacheability. Measured against production 2026-08-18: `/shows` already sets
`_SHOW_EP_CACHE` (`routers/browse.py:90`), because it already carries
`my_rating`. The mark is free there.

The inherited error is worth naming so it is not copied forward again: the
router-level default has always been `private`, never `public`, and the helper
is `_set_browse_cache`, not the `set_cache_control` NEU-1059's description named.
CLAUDE.md recorded both wrongly and was corrected 2026-08-18.

### 3.2 `/similar` is the one genuine decision, and `my_rating` rides on it

NEU-1053 gave two reasons that route carries no per-user field: filling one costs
a query, **and** it costs the shared cacheability of a body that is byte-identical
for every viewer. The second is the stronger of the two, and this spec spends it.

`no-store` rather than merely `private` for the reason `_SHOW_EP_CACHE` exists:
the mark is not only per-user but *mutable by that user*, and there is no way to
invalidate the browser's HTTP cache — so any `max-age` lets a React Query refetch
after a My Shows toggle read the pre-toggle body out of the browser and revert
the optimistic update. That is a visibly broken toggle, not a staleness
nuisance. `/trending` and `/anticipated` carry the identical mark and resolved it
the identical way.

Once that is spent, **`my_rating` follows**, because only NEU-1053's weaker
reason still stands against it. Leaving it out would keep the Similar tab as the
one grid in the app where the same show shows your rating on every other surface
and not on this one — which is the class of inconsistency this milestone exists
to end. The cost is one further query on a route that returns at most twelve
rows.

The consequence for the route's docstring: the paragraph beginning *"**The
payload carries no per-user field**, which is what lets this route keep the
router-level `private, max-age=300`"* is now false and is rewritten to record the
trade actually made. Leaving a docstring that argues against the code is worse
than having no docstring.

### 3.3 The rule loses its standing example, and says so

CLAUDE.md's browse-cache paragraph uses `/similar` as the live example of
*"adding a per-user field to a route that has kept the default is a cache
change, not just a payload change."* After this ticket the rule has no live
example: what remains behind the router-level `private, max-age=300` is
`/genres`, `/networks`, `/shows/{id}/cast`, `/shows/{id}/crew`, `/people`,
`/people/{id}`, `/people/{id}/credits`, `/episodes/{id}/guest-cast` and
`/episodes/{id}/crew` — catalog reference data with no per-user field to gain.
The paragraph is rewritten to state that rather than to keep citing a route that
no longer demonstrates it. The rule itself is unchanged and still binds the next
route that grows one.

## 4. Query budget

Two hydration helpers, one query each, both already in use:
`show_membership_repo.tracked_show_ids` for the mark and
`browse_queries.hydrate_my_ratings` for the rating. Neither is merged into a
single cross-table helper — they read `app.user_show_watch` and
`app.user_show_rating`, the thin per-table repos are the existing pattern, and a
combined `FULL JOIN` helper would rewrite `/trending`'s working code for one
round trip.

| Route | `catalog` queries | `app` queries added | Scales with page size? |
| -- | -- | -- | -- |
| `GET /shows` | 4 (unchanged) | +1 (mark) | no |
| `GET /shows/{id}/similar` | 2 (unchanged) | +2 (mark, rating) | no |

**The criterion is that nothing moves with the page size, not an absolute
delta.** NEU-1185's AC 2 asked for "exactly one additional query"; that no longer
describes `/similar` once `my_rating` lands, and the invariant that actually
protects the route is the one an accidental per-row `.get()` would break.

**`test_get_shows_issues_a_fixed_number_of_queries_whatever_the_page_size` needs
no edit.** The ticket predicted it would move from four to five. It will not:
that test counts only statements containing `"catalog."`, deliberately excluding
`app` tables — its own docstring says so, and `hydrate_my_ratings` already rides
free on that rule. `user_show_watch` is an `app` table, so the mark adds a
statement to the `len(for_one) == len(for_ten)` assertion (which stays equal) and
zero to the pinned `== 4`. The test gains one docstring line saying why the mark
is outside the number; it does not gain an assertion.

`/similar` gets the same invariant asserted in its own way: the statement count
must not move with the number of recommendations the show has.

## 5. Frontend consumption (NEU-1186)

### 5.1 Types mirror the base one-for-one

```ts
interface MarkedShow extends ShowSummary { in_my_shows: boolean }
interface TrendingShow extends MarkedShow {}
interface AnticipatedShow extends MarkedShow {}
interface BrowseShow extends MarkedShow {}
interface SimilarShow extends MarkedShow {}

ShowListPage.items: BrowseShow[]
useSimilarShows(): SimilarShow[]
```

`src/api/types.ts` documents itself as mirroring the wire contract, so the two
class trees stay legible against each other.

**`ShowGrid`, `ShowCard` and the two call sites need no code change.** The grid
already accepts `(ShowSummary & { in_my_shows?: boolean })[]` and threads
`s.in_my_shows` to the card; the card already renders the mark through
`ShowPoster`, which owns its corner (NEU-1183 §3.4 — facts on top, controls on
the bottom). The mark's picture is `InMyShowsBadge`, shared with every other
surface: one claim, one picture (NEU-1057). `SearchOverlay.tsx:219` and
`SimilarShows.tsx:37` are unchanged lines. The ticket is DTOs, hook return
types, invalidation and tests.

### 5.2 Invalidation, not `staleTime`

Both bodies are now per-user *and* user-mutable, so both must be refetched after
a mutation that changes them:

| Mutation | Invalidates |
| -- | -- |
| My Shows add/remove (`invalidateAll`) | **`["shows"]`, `["show-similar"]`** — new — alongside the existing `["trending"]`, `["anticipated"]`, `["my-shows"]`, … |
| `useShowRating` | `["shows"]` (existing) **and `["show-similar"]`** — new, because `/similar` now carries `my_rating` |

**`staleTime` stays at five minutes on both hooks.** Invalidation is the
mechanism that actually fixes the stale mark — `staleTime: 0` alone only
refetches on mount, which is why `invalidateAll` already invalidates trending and
anticipated despite their zero. This is precisely how `my_rating` on `/shows` was
already handled, and search is the app's chattiest query, firing one request per
debounced keystroke. The Discover hooks' `staleTime: 0` is a separate choice
about two rarely-mounted panes, not a rule that follows from `no-store`; that
distinction is worth keeping straight, because reading it as a rule would put a
refetch on every open of the search overlay for nothing.

`useSimilarShows`' docstring line *"`genres`, `network` and `my_rating` come back
empty by design"* is rewritten: the first two still do, the third does not.

### 5.3 Grid only; the list view is NEU-1188's

Search offers a grid/list toggle and defaults to `grid`
(`SearchOverlay.tsx:113`), so the reproduction case in §1 is satisfied by the
grid path alone. `ShowList` renders **no badge of any kind** today — no mark, no
own rating, no aggregate — and NEU-1188 AC 1 already owns exactly that row:
*"Search list rows carry the viewer's rating, the TMDB average and the library
mark."*

Landing the mark alone in the list row would give it a library badge and still no
rating, which is a *new* inconsistency rather than one fewer, and it would
pre-empt the placement decisions NEU-1188 has to make for a dense row under
NEU-1182's rating vocabulary. So the mark is grid-only here, and **NEU-1186's PR
states the deferral explicitly** — which is the option that ticket's own scope
note offers.

## 6. The grid audit, and the one recorded exception

NEU-1184's definition of done is *"every grid built on `ShowCard` marks a tracked
show, or has a recorded reason not to."* After both children:

| `ShowGrid` call site | Marked | Note |
| -- | -- | -- |
| `SearchOverlay` (grid view) | ✅ | this story |
| `SimilarShows` | ✅ | this story |
| `Trending` | ✅ | NEU-1056 |
| `Anticipated` | ✅ | NEU-1059 |
| `RecommendedForYou` | — | recorded reason, below |

`RecommendedForYou` renders `Recommendation[]`, which carries no mark, and
**`RecommendationOut` does not gain one.** `GET /me/recommendations` suppresses
any show the viewer already has a record for, as a live anti-join over
`recommendations/exclusion.py`'s sources (NEU-1175, NEU-1178), so `in_my_shows`
would be `false` on every card ever served there — a field asserting nothing, on
the one route that already pays a suppression join to guarantee it.

The absence is the rule holding, not a gap, and NEU-1186 records that in
`RecommendedForYou`'s docstring so the next reader does not have to leave the
file to find out why it differs from its four siblings. That single line is what
closes the story's DoD with exactly two children.

## 7. What this story does not do

* **No add/remove control on search results or the Similar tab.** Four
  treatments of that control exist across the app and NEU-1187 converges them;
  adding a fifth here would be work to undo one ticket later. `addable` stays
  what it is — an opt-in seam only `RecommendedForYou` passes.
* **No mark on `GET /shows/{id}`.** The detail page derives membership from
  `useMyShows()` and needs nothing from the payload; adding an uncomputed field
  there is the hoisting mistake §2.1 declines.
* **No change to `/trending` or `/anticipated` behaviour.** Their types are
  re-parented and their served bodies are byte-identical.
* **No change to `/similar`'s list**: same twelve, same rank order, same
  read-time filters, same `200 []` and same `404`.
* **No `genres` / `network` hydration on `/similar`.** The cheaper reasoning is
  untouched, and no consumer wants them.
* **No new spec in the umbrella `docs/`, and no plan file.** The two children are
  built against this document; their acceptance criteria are the steps.

## 8. Where it lives

| Piece | File |
| -- | -- |
| Routes | `src/tvbf/routers/browse.py` — `list_shows_route`, `get_show_similar_route` |
| Shapes | `src/tvbf/catalog/schemas.py` — `MarkedShowOut`, `BrowseShowOut`, `SimilarShowOut`, `TrendingShowOut`, `AnticipatedShowOut` |
| The mark | `src/tvbf/app/repos/show_membership_repo.py` — `tracked_show_ids` |
| The rating | `src/tvbf/catalog/browse_queries.py` — `hydrate_my_ratings` |
| The list | `src/tvbf/catalog/browse_queries.py` — `list_shows`, `list_similar_shows`, `SIMILAR_LIMIT` |
| Cache override | `src/tvbf/routers/browse.py` — `_SHOW_EP_CACHE` |
| Contract tests | `tests/integration/routers/test_browse.py` |
| Frontend DTOs | `tvbf-frontend/src/api/types.ts` — `MarkedShow`, `BrowseShow`, `SimilarShow` |
| Frontend hooks | `tvbf-frontend/src/api/shows.ts` — `useShows`, `useSimilarShows` |
| Frontend invalidation | `tvbf-frontend/src/api/me.ts` — `invalidateAll`, `useShowRating` |
| The recorded exception | `tvbf-frontend/src/components/discover/RecommendedForYou.tsx` |

## 9. Acceptance, restated against this contract

**NEU-1185 (backend)**

1. `GET /shows` items and `GET /shows/{id}/similar` rows carry `in_my_shows` for
   the authenticated viewer; `/similar` also carries `my_rating`.
2. `in_my_shows` is declared once, on `MarkedShowOut`, required and without a
   default; `/trending` and `/anticipated` serve byte-identical bodies.
3. A tracked show is **marked, never filtered** — it still appears in search
   results and in a similar list.
4. Two viewers of the same list see different marks and ratings and the same
   shows in the same order.
5. `/shows/{id}/similar` answers `Cache-Control: private, no-store`; `/shows`
   still answers `private, no-store`; the `/similar` docstring paragraph that
   argued for the router-level header is rewritten.
6. Neither route's statement count moves with page size or list length; the
   `/shows` query-count test is unedited and gains the docstring line from §4.
7. CLAUDE.md's browse-cache paragraph is updated per §3.3, and its endpoint-
   surface entries for both routes name the new fields.

**NEU-1186 (frontend)**

1. A tracked show in the search results **grid** carries the library mark.
2. A tracked show in the **Similar** tab carries the library mark and the
   viewer's rating.
3. The mark is `InMyShowsBadge`, placed by `ShowPoster` — no new picture and no
   call-site position.
4. Reproduction case passes: search `the bear` with *The Bear* in My Shows shows
   the mark alongside ★4.5 and ★4.1.
5. `invalidateAll` invalidates `["shows"]` and `["show-similar"]`;
   `useShowRating` invalidates `["show-similar"]`; both `staleTime` values are
   unchanged.
6. MSW handlers and DTOs updated; per route, one test with a marked and an
   unmarked row **in the same payload**.
7. The PR states that the search **list** view is deferred to NEU-1188.
8. `RecommendedForYou`'s docstring records why it passes no mark.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
