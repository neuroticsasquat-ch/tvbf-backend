# NEU-1112 — The recommendations page contract

**Ticket:** [NEU-1112](https://linear.app/neuroticsasquatch/issue/NEU-1112/add-get-merecommendations)
**Repo:** `tvbf-backend`
**Project:** TVBF: Personalized Recommendations · Milestone 5, The Discover surface
**Project spec:** `docs/specs/tvbf-personalized-recommendations-project-spec.md` (umbrella) §7, §8, §11
**Related:** NEU-1108 (the current-set repo function), NEU-1114 (the frontend half), NEU-1109 (the weekly pass that writes what this serves), NEU-1175 and NEU-1178 (the never-recommend set and the dismiss endpoint, §4.1 and §5.1)
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
      "rank": 1
    }
  ]
}
```

Each item is **`ShowSummary` flattened, plus `rank`**. Flattened rather than
nested under a `show` key, unlike `MyShowEntry` / `WatchNextEntry` /
`UpcomingEntry`: those carry per-show *progress*, which is a second object with
its own identity, where a recommendation carries a position.
`ShowSummary` already mirrors exactly across both repos, so the frontend adds one
type and `ShowGrid` / `ShowCard` stay usable unchanged.

* **`rank`** is the model's own ordering, 1-based, and the array is returned in
  it. The server never re-sorts, and neither should a client — the ranking is the
  only ordering in the payload that carries information.

### `reason` was removed from this payload on 2026-08-17

It used to be here, described as model-authored prose rendering as plain text.
The card has **one truncated 10px line** for it — not room for a sentence — so
serving it put prose on the wire that nothing displayed.

It is **still asked for and still stored** (`app.user_recommendation.reason`,
plus the whole answer in `user_recommendation_set.raw_response`), because
removing it from the *prompt* is a separate and riskier change: `reason` is
where the model puts its explanations, and a production run that day showed what
happens when it has nowhere to put them — they land in `title`, which resolves
to nothing. Treat the stored value as diagnostic rather than as content.

A client must not reintroduce `reason`; it is not served.

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

### 4.1 A show in the never-recommend set is suppressed (NEU-1175, NEU-1178)

The list is **the top twelve stored suggestions the viewer may still be
recommended**. A stored set is immutable, so before this the viewer could add a
recommended show to My Shows and keep watching it occupy a card until Sunday's
pass superseded the whole set — up to seven days.

The never-recommend set has five sources. Four are project spec §8's record
sources; the fifth is a dismissal (§5.1), which is *not* one — it can name a
show the viewer has never seen at all:

| Source | Created by |
| -- | -- |
| My Shows membership | adding the show |
| A show rating | rating the show |
| Any episode watch | marking any episode, season or the show watched |
| Any episode rating | rating any episode |
| A dismissal | `POST /me/recommendations/{show_id}/dismiss` |

**A client must not re-implement this rule** — it is one definition on the
server (`recommendations/exclusion.py`), used both by the weekly pass's
never-recommend list and by this read, and a client copy would be a second
expression of it that drifts. What a client *may* do with the list is decide
when to refetch: any of the five actions above can change this payload, so the
grid is stale after one until it is fetched again.

Two consequences of the suppression being a live join rather than a stored flag:

* **`rank` values stay non-contiguous.** Nothing is renumbered — rank is the
  model's own ordering, and §2 already says a client displays the value rather
  than its index. A response of ranks `4, 5, 7, 9…` is normal.
* **Fewer than twelve is a normal answer.** The 25-asked-for headroom is the
  mechanism; when it runs out, fewer cards is the answer. Nothing is backfilled
  from an older set, and an empty list is still `200
  {"recommendations": []}` — never a `204`, never an error.

Removing the record brings the suggestion back, provided no *other* record for
that show remains: un-adding a show you watched three episodes of does not
unmake those episodes. A **dismissal** is the exception, and by design — nothing
removes that row, so a dismissed show does not come back (§5.1).

## 5. What this route does not do

* **No `limit` parameter.** Twelve is a design decision (§11), not a preference.
* **No sort or filter controls.** The ranking is the model's; letting a user
  re-sort discards the only ordering that carries information.
* **No pagination.** The whole surface is one grid of twelve.
* **No generation trigger.** Regenerating an account is `POST
  /admin/recommendations` (NEU-1110), bearer-token, or the Sunday schedule.
* **No upstream call of any kind.** Everything here is a local read; ADR-0002
  holds without exception.

### 5.1 Dismissing a recommendation (NEU-1178)

```
POST /me/recommendations/{show_id}/dismiss   →  204 No Content
```

Session cookie **and** `X-CSRF-Token`, like every other mutating `/me` route. No
request body and no response body — the client refetches
`GET /me/recommendations` afterwards.

* **`204` on success, and on every repeat.** The write is
  `ON CONFLICT DO NOTHING`, so dismissing the same show twice leaves one row and
  answers `204` both times. A client may fire it without checking first.
* **`404 {"detail": "not_found"}`** for a show id no catalog row has, and that is
  the only `404`. An `adult` or tombstoned show is dismissible like any other.
* **The show need not be in the current set.** The endpoint does not look at the
  set at all: the never-recommend list is about future passes as much as the
  current grid, so dismissing a show found by search is coherent.
* **There is no un-dismiss.** "Never again" is the feature. A surface listing a
  user's dismissals, and a way back from one, are a later ticket (NEU-1177) —
  which is also why dismissal is scoped to this route and the weekly pass, and
  why trending, most anticipated, similar shows, search and browse are
  deliberately **unaffected**. A user must still be able to find a show they
  dismissed.
* **A dismissal is not a taste signal.** It never reaches the model as a
  negative opinion: it lands in the payload's `exclude` group, which says only
  "do not name this", and never in `not_liked`, which is something the model
  generalises from.
* **The grid is stale afterwards**, like any other §4.1 action — refetch.

## 6. Where it lives

| Piece | File |
| -- | -- |
| Route | `src/tvbf/routers/me.py` — `list_my_recommendations`, `dismiss_recommendation` |
| Shapes | `src/tvbf/app/schemas.py` — `RecommendationOut`, `RecommendationsOut` |
| Cap + hydration | `src/tvbf/app/services/recommendation_service.py` — `DISPLAY_LIMIT` |
| The current set | `src/tvbf/app/repos/recommendation_repo.py` (NEU-1108) |
| The never-recommend rule | `src/tvbf/recommendations/exclusion.py` (NEU-1175, NEU-1178) |
| The dismissal write | `src/tvbf/app/repos/recommendation_dismissal_repo.py` (NEU-1178) |
| Contract tests | `tests/integration/routers/test_me_recommendations.py` |
