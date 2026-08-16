# NEU-1112 — The `GET /me/recommendations` page contract

**Ticket:** [NEU-1112](https://linear.app/neuroticsasquatch/issue/NEU-1112/add-get-merecommendations)
**Repo:** `tvbf-backend`
**Project:** TVBF: Personalized Recommendations · Milestone 5, The Discover surface
**Project spec:** `docs/specs/tvbf-personalized-recommendations-project-spec.md` (umbrella) §7, §8, §11
**Related:** NEU-1108 (the current-set repo function), NEU-1114 (the frontend half), NEU-1109 (the weekly pass that writes what this serves)
**Status:** approved for implementation; implemented by NEU-1112

This spec lives in-repo rather than in the umbrella `docs/` because it is a
**cross-repo contract**: `tvbf-frontend`'s NEU-1114 and TMDB Discovery's tickets
cite the response shape below by URL, and the umbrella `docs/` is not a git repo,
so nothing in it can be linked or diffed (CLAUDE.md's cross-repo-citation rule).
It records the contract and the reasons behind it; the design it realizes is the
project spec, which stays in the umbrella.

---

## 1. The route

```
GET /me/recommendations
```

* **Auth:** `get_current_user` — the session cookie, same as every other `/me`
  route. No CSRF (it is a read).
* **Caching:** `Cache-Control: no-store`, matching `/me/feed`. Per-user content
  behind a session cookie, and the weekly pass replaces it out from under any
  cache.
* **Query parameters:** none. There is no `limit`, no `sort` and no pagination —
  see §4.

## 2. The response

`200` with:

```json
{
  "recommendations": [
    {
      "id": 811001,
      "name": "Severance",
      "type": null,
      "status": "Returning Series",
      "language": "en",
      "premiered": "2022-02-18",
      "ended": null,
      "image_medium": "https://image.tmdb.org/t/p/w342/abc.jpg",
      "image_original": "https://image.tmdb.org/t/p/original/abc.jpg",
      "network": { "id": 902, "name": "Apple TV+" },
      "web_channel": null,
      "genres": ["Drama"],
      "matched_aka": null,
      "rating_average": 8.4,
      "my_rating": null,
      "rank": 1,
      "reason": "One sentence of model-authored prose."
    }
  ]
}
```

Each item is **`ShowSummary` flattened, plus `rank` and `reason`**. Flattened
rather than nested under a `show` key, unlike `MyShowEntry` / `WatchNextEntry` /
`UpcomingEntry`: those carry per-show *progress*, which is a second object with
its own identity, where a recommendation carries a sentence and a position.
`ShowSummary` already mirrors exactly across both repos, so the frontend adds one
type and `ShowGrid` / `ShowCard` stay usable unchanged.

* **`rank`** is the model's own ordering, 1-based, and the array is returned in
  it. The server never re-sorts, and neither should a client — the ranking is the
  only ordering in the payload that carries information.
* **`reason`** is model-authored prose. It **renders as plain text, never
  markup** (project spec §7). It can assert things about a show that are untrue,
  and can describe a show subtly different from the one resolution landed on.

Ranks are the stored ranks and are therefore **not guaranteed contiguous or to
start at 1**: a filtered row (§3) takes its rank with it. A client displaying
`rank` should display the value, not its index.

## 3. Degradation — what "nothing to show" means

**Empty is `200` with `{"recommendations": []}`, never `204`.** The frontend
distinguishes "no recommendations" from "the request failed" by status code, and
a 204 collapses the two into one thing it cannot render differently. Per project
spec §11 the surface's response to an empty list is to **render no section at
all** — no empty state, no nudge, no spinner, no error.

Four distinct situations all produce that empty list, deliberately:

| Situation | Why it is empty |
| -- | -- |
| The user has never had a set generated | Nothing to read |
| The newest set is `insufficient_history` | Below the generation floor (§5.4) |
| The newest set is `no_matches` | Ran, resolved nothing |
| The newest set is `failed` | The provider failed |

The last three are **invisible to this route by construction**, not by a check
here: `recommendation_repo` defines the current set as the newest set with
`status = 'succeeded'` (NEU-1108). The consequence worth stating plainly is the
one that makes an unhappy run non-destructive — **a failed run at 4am on Sunday
leaves last week's recommendations standing** rather than blanking the section.

## 4. The cap and the filters are the server's, not the client's

The server returns **at most 12** items, and applies the `adult` and
`deleted_upstream_at` filters at read time.

**The client never filters and never slices.** The moment it did, the two would
disagree about what "twelve" means the first time a tombstone landed — because
the filters run **before** the cap, not after it. A set stores 25 (project spec
§7) so that twelve *survivors* remain after resolution failures, the
never-recommend filter, and read-time tombstoning have each taken their share. A
set generated in March can name a show tombstoned in June; the headroom is what
absorbs that, and a client slicing a pre-filtered list to 12 would show 10.

Read-time rather than write-time filtering is deliberate for the same reason
(project spec §8): a write-time copy would be the weaker half of the filter and
would make a resurrected show permanently unrecommendable.

## 5. What this route does not do

* **No `limit` parameter.** Twelve is a design decision (§11), not a preference.
* **No sort or filter controls.** The ranking is the model's; letting a user
  re-sort discards the only ordering that carries information.
* **No pagination.** The whole surface is one grid of twelve.
* **No generation trigger.** Regenerating an account is `POST
  /admin/recommendations` (NEU-1110), bearer-token, or the Sunday schedule.
* **No dismissal / "not interested".** Out of scope with reasons (§13).
* **No upstream call of any kind.** Everything here is a local read; ADR-0002
  holds without exception.

## 6. Where it lives

| Piece | File |
| -- | -- |
| Route | `src/tvbf/routers/me.py` — `list_my_recommendations` |
| Shapes | `src/tvbf/app/schemas.py` — `RecommendationOut`, `RecommendationsOut` |
| Cap + hydration | `src/tvbf/app/services/recommendation_service.py` — `DISPLAY_LIMIT` |
| The current set | `src/tvbf/app/repos/recommendation_repo.py` (NEU-1108) |
| Contract tests | `tests/integration/routers/test_me_recommendations.py` |
