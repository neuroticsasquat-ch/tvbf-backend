# NEU-1059 — The `GET /anticipated` contract

**Ticket:** [NEU-1059](https://linear.app/neuroticsasquatch/issue/NEU-1059/backend-get-anticipated)
**Repo:** `tvbf-backend`
**Project:** TVBF: TMDB Discovery · Milestone 4, Most anticipated
**Project spec:** `docs/specs/tvbf-tmdb-discovery-project-spec.md` (umbrella) §4
**Related:** NEU-1058 (the query this serves), NEU-1060 (the frontend half), NEU-1056 (`GET /trending`, the sibling marked surface)
**Status:** approved for implementation; implemented by NEU-1059

This spec lives in-repo rather than in the umbrella `docs/` because it is a
**cross-repo contract**: `tvbf-frontend`'s NEU-1060 cites the response shape and
the degradation rule below by URL, and the umbrella `docs/` is not a git repo, so
nothing in it can be linked or diffed (CLAUDE.md's cross-repo-citation rule). It
records the contract and the reasons behind it; the design it realizes is the
project spec, which stays in the umbrella.

---

## 1. The route

```
GET /anticipated
```

* **Auth:** `get_current_user` — the session cookie, like every other browse
  route. Invite-only beta means even the catalog is not public.
* **Caching:** `Cache-Control: private, no-store`, the same override `/trending`
  and the show and episode routes take. The ticket asked for
  `public, max-age=300` *and* for entries to be marked with the viewer's My
  Shows membership, and neither half of that survives the mark. `public` is the
  lesser problem — a shared cache authorized to fan the body out serves one
  account's marks to another — and the router-level `private` already fixes it.
  **It is the `max-age` that cannot stay**: the mark is not merely per-user but
  mutable by that user, so any max-age lets a My Shows toggle be followed by a
  refetch that reads the pre-toggle body out of the browser cache and reverts
  the optimistic update. That is the reason the show and episode routes carry
  `no-store`, and `/trending` carries the identical mark and resolved it the
  identical way. The ticket's `set_cache_control` helper no longer exists under
  that name; the router-level default is `_set_browse_cache` and the override is
  `_SHOW_EP_CACHE`.
* **Query parameters:** none. There is no `limit` and no `window` — see §5.

## 2. The response

`200` with a **bare array**:

```json
[
  {
    "id": 811001,
    "name": "Lanterns",
    "type": null,
    "status": "Returning Series",
    "language": "en",
    "premiered": "2027-02-18",
    "ended": null,
    "image_medium": "https://image.tmdb.org/t/p/w342/abc.jpg",
    "image_original": "https://image.tmdb.org/t/p/original/abc.jpg",
    "network": null,
    "web_channel": null,
    "genres": [],
    "matched_aka": null,
    "rating_average": null,
    "my_rating": null,
    "in_my_shows": true
  }
]
```

Each entry is **`ShowSummary` flattened, plus `in_my_shows`** — the shape
`RecommendationOut` established and `TrendingShowOut` reused, and for its reason:
`ShowGrid` and `ShowCard` already take a `ShowSummary`, so a wrapper type would
cost the frontend something for a single boolean.

* **A bare array rather than an object**, unlike `/trending` and
  `/me/recommendations`. Those wrap because they carry a second field — a
  `captured_at`, a set. Nothing is stored here, so there is no second field and
  the wrapper would hold only the list. `/shows/{id}/similar` answers the same
  way.
* **Ordered most-anticipated first** — `popularity DESC`, unscored shows last,
  the show id breaking ties so a client re-requesting gets the same order. The
  server never re-sorts and the position is not exposed as a number.
* **`genres` is always `[]` and `network` always `null`.** `ShowCard` renders
  neither, so hydrating them would be two more round trips for fields nothing
  displays. `/trending` and `/shows/{id}/similar` make the same trade. A
  consumer wanting the list-view shape (`ShowList` reads both) would need them
  hydrated here first.
* **`my_rating` is always `null`**, unlike on `/trending`. Every show on this
  list premieres in the future, so a rating for one is a rating for something
  nobody has seen — trending's argument for filling it (the badge would be
  missing on Discover and present everywhere else for the same show) does not
  reach a list of unpremiered shows. `rating_average` is TMDB's and is served as
  stored, which for an unpremiered show is usually null for the same reason.

## 3. Freshness is structural, and there is no cutoff

**The list is a live query, not a snapshot** (project spec §4, NEU-1058). Three
rules `/trending` needs are absent here rather than solved:

| `/trending` needs | `/anticipated` does not, because |
| -- | -- |
| A rule for entries that have since premiered | `first_air_date >= current_date` is evaluated on the read |
| A rule for what a failed run leaves behind | There is no run |
| A seven-day staleness cutoff | Nothing is stored, so there is no `captured_at` to measure |

The staleness cutoff has a second, independent reason not to exist: "anticipated"
carries no present-tense promise that a week-old answer would falsify, the way
"trending right now" does.

**So the SPA needs no staleness handling on this surface at all**, and must not
invent one — there is no timestamp in the payload to build it from.

## 4. Degradation — what "nothing to show" means

**Empty is `200 []`, never a `204` and never a `404`**, on
`/shows/{id}/similar`'s and `/me/recommendations`' reasoning: the frontend
distinguishes "nothing to show" from "the request failed" by status code, and a
204 collapses the two into one thing it cannot render differently. The surface's
response to an empty list is to render no tab content — no empty state, no
error.

In practice the list is empty only if the mirror holds no future-dated show at
all; production holds 408.

## 5. What this route does not do

* **No filtering of tracked shows.** Shows in the viewer's My Shows are
  **marked, never removed** — a list of what is coming is a claim about the
  world, and seeing something you are already waiting for in it is a feature.
  `in_my_shows` is how the surface renders that differently.
* **No `limit` and no `window` parameter.** Both are server-side config with
  measured defaults — 24 entries, 365 days (`ANTICIPATED_LIMIT`,
  `ANTICIPATED_WINDOW_DAYS`, NEU-1058). Neither is load-bearing enough to
  publish, and publishing either would let a client ask for a list the index and
  the cache were not sized for.
* **No sort or filter controls.** Popularity within the window is the only
  ordering that carries information here.
* **No `vote_count` floor.** Of 408 future-dated shows in production four carry
  any votes; unpremiered shows do not get voted on, so a floor empties the
  surface rather than cleaning it (project spec §4).
* **No `status` filter.** *Lanterns* is a `Returning Series` with a future first
  air date and belongs on the list. An **undated** show never appears, whatever
  its status.
* **No upstream call.** Everything is a local read of `catalog.show`; ADR-0002
  holds without exception, and since ADR-0007 `/discover/tv` is a query against
  the catalog that is already ours.

## 6. Where it lives

| Piece | File |
| -- | -- |
| Route | `src/tvbf/routers/browse.py` — `get_anticipated_route` |
| Shape | `src/tvbf/catalog/schemas.py` — `AnticipatedShowOut` |
| Query + the window and length | `src/tvbf/catalog/browse_queries.py` — `list_anticipated_shows`, `ANTICIPATED_WINDOW_DAYS`, `ANTICIPATED_LIMIT` (NEU-1058) |
| The mark | `src/tvbf/app/repos/show_membership_repo.py` — `tracked_show_ids` |
| Contract tests | `tests/integration/routers/test_anticipated.py` |
