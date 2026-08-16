# NEU-1056 — The `GET /trending` contract

**Ticket:** [NEU-1056](https://linear.app/neuroticsasquatch/issue/NEU-1056/backend-get-trending-with-server-side-7-day-staleness-cutoff)
**Repo:** `tvbf-backend`
**Project:** TVBF: TMDB Discovery · Milestone 3, Trending
**Project spec:** `docs/specs/tvbf-tmdb-discovery-project-spec.md` (umbrella) §3
**Related:** NEU-1055 (the daily snapshot job this serves), NEU-1057 (the frontend half), NEU-1053 (`/shows/{id}/similar`, the sibling read route)
**Status:** approved for implementation; implemented by NEU-1056

This spec lives in-repo rather than in the umbrella `docs/` because it is a
**cross-repo contract**: `tvbf-frontend`'s NEU-1057 cites the response shape and
the degradation rule below by URL, and the umbrella `docs/` is not a git repo, so
nothing in it can be linked or diffed (CLAUDE.md's cross-repo-citation rule). It
records the contract and the reasons behind it; the design it realizes is the
project spec, which stays in the umbrella.

---

## 1. The route

```
GET /trending
```

* **Auth:** `get_current_user` — the session cookie, like every other browse
  route. Invite-only beta means even the catalog is not public.
* **Caching:** `Cache-Control: private, no-store`, the same override the show and
  episode routes take. The payload carries a per-user field (`in_my_shows`), and
  a `max-age` on a per-user body is how a browser cache ends up showing one
  account's marks to another tab.
* **Query parameters:** none. There is no `limit`, no `window` and no
  pagination — see §5.

## 2. The response

`200` with:

```json
{
  "captured_at": "2026-08-16T04:00:11.481Z",
  "shows": [
    {
      "id": 811001,
      "name": "Lanterns",
      "type": null,
      "status": "Returning Series",
      "language": "en",
      "premiered": "2026-02-18",
      "ended": null,
      "image_medium": "https://image.tmdb.org/t/p/w342/abc.jpg",
      "image_original": "https://image.tmdb.org/t/p/original/abc.jpg",
      "network": null,
      "web_channel": null,
      "genres": [],
      "matched_aka": null,
      "rating_average": 8.4,
      "my_rating": 4.5,
      "in_my_shows": true
    }
  ]
}
```

Each entry is **`ShowSummary` flattened, plus `in_my_shows`** — the shape
`RecommendationOut` established, and for its reason: `ShowGrid` and `ShowCard`
already take a `ShowSummary`, so a wrapper type would cost the frontend
something for a single boolean.

* **`shows` is in TMDB's rank order** and the server never re-sorts it. The rank
  itself is not exposed: it is a position in a list the client receives in that
  order, and unlike a recommendation's `rank` there is no second ordering it
  could be confused with. Stored ranks have gaps — the job drops an entry it
  cannot resolve to a `catalog.show` rather than renumbering (NEU-1055) — which
  is a second reason not to publish a number a client might render.
* **`genres` is always `[]` and `network` always `null`.** `ShowCard` renders
  neither, so hydrating them would be two more round trips for fields nothing
  displays. `/shows/{id}/similar` makes the same trade. A consumer wanting the
  list-view shape (`ShowList` reads both) would need them hydrated here first.
* **`my_rating` *is* filled**, unlike on `/shows/{id}/similar`. That route
  declines it to keep a body identical for every viewer and therefore cacheable;
  this one already carries `in_my_shows`, so there is no such body to protect and
  the badge would otherwise be missing on Discover and present everywhere else
  for the same show.

## 3. Staleness is the server's rule, and only the server's

**A snapshot older than seven days serves an empty list.** The cutoff is measured
against `captured_at`, which NEU-1055 stamps *before* the request to TMDB goes
out, so it describes the list rather than the bookkeeping that stored it.

**The SPA must never re-implement this.** A rule enforced in two places drifts,
and what it drifts into is week-old rows under a label reading "trending right
now". Silent staleness under a present-tense label is worse than an absent
section, and this is one of the few surfaces whose data has an expiry baked into
its own name.

Two consequences the payload is shaped to guarantee:

* **`captured_at` is null exactly when `shows` is empty.** It describes the list
  served. A stale snapshot's timestamp is *not* reported — reporting it would
  hand a client everything it needs to re-derive the cutoff, which is the one
  thing this route exists to own.
* **A stale snapshot and an empty table are the same body.** The client cannot
  tell them apart and has no reason to want to; both render as no section.

The cutoff lives in exactly one function — `browse_queries.get_trending_snapshot`
— and is applied *in the query*, so there is no path through that module which
returns a row past it.

## 4. Degradation — what "nothing to show" means

**Empty is `200` with `{"captured_at": null, "shows": []}`, never a `204` and
never a `404`**, on `/me/recommendations`' reasoning: the frontend distinguishes
"nothing to show" from "the request failed" by status code, and a 204 collapses
the two into one thing it cannot render differently. The surface's response to an
empty list is to **render no tab content** — no empty state, no error, and in
particular the user is never shown the word "stale" (NEU-1057).

Four situations produce that empty list, deliberately indistinguishable:

| Situation | Why it is empty |
| -- | -- |
| The job has never run | No rows |
| The snapshot is older than seven days | §3 |
| The job ran and resolved nothing | It writes nothing, leaving the previous snapshot — which then ages out under §3 |
| Every entry is `adult` or tombstoned | Filtered at read time (§5) |

The third is the one worth stating plainly: a failed run leaves the previous
snapshot intact rather than truncating (NEU-1055), and the seven-day cutoff is
what bounds how long that is allowed to matter.

## 5. What this route does not do

* **No filtering of tracked shows.** Shows in the viewer's My Shows are
  **marked, never removed** — trending is a claim about the world, and seeing
  your own show in it is a feature. `in_my_shows` is how the surface renders that
  differently.
* **No `limit`.** The snapshot is at most twenty rows by construction.
* **No sort or filter controls.** TMDB's ranking is the only ordering that
  carries information here.
* **No upstream call.** Everything is a local read of `catalog.trending_show`;
  ADR-0002 holds without exception. The one request a day belongs to the job.
* **No write-time `adult` / `deleted_upstream_at` filter.** Both run at read
  time, on NEU-1053's and NEU-1108's precedent — a snapshot taken this morning
  can name a show tombstoned this afternoon, and a write-time copy of the rule
  would leave a resurrected show invisible until the next snapshot.

## 6. Where it lives

| Piece | File |
| -- | -- |
| Route | `src/tvbf/routers/browse.py` — `get_trending_route` |
| Shapes | `src/tvbf/catalog/schemas.py` — `TrendingShowOut`, `TrendingOut` |
| Query + the cutoff | `src/tvbf/catalog/browse_queries.py` — `get_trending_snapshot`, `TRENDING_MAX_AGE` |
| The mark | `src/tvbf/app/repos/show_membership_repo.py` — `tracked_show_ids` |
| The snapshot it reads | `src/tvbf/tmdb/trending.py` (NEU-1055) |
| Contract tests | `tests/integration/routers/test_trending.py` |
