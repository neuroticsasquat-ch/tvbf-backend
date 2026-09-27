# tvbf: Popular with Friends — project spec

**Linear project:** tvbf: Popular with Friends (P-NEU-93; created as "tvbf: Trending with Friends") · initiative TV BingeFriend · team Neuroticsasquatch
**Repos:** `tvbf-backend` (this repo), `tvbf-frontend`
**Decisions recorded:** [ADR-0015](../adr/0015-friend-scoped-aggregates-read-the-activity-ledger.md); vocabulary in `CONTEXT.md` § The app (*Activity*, *Sharing switches*, *Trending*, *Popular with friends*)
**Written:** 2026-09-27, from the `/projectit` grilling session.

This is the project-wide spec every ticket in the project is implemented against unless a
per-ticket spec (`docs/specs/NEU-xxxx-*.md`) says otherwise. Frontend tickets cite it by this
path from `tvbf-frontend`.

## 1. Purpose

Show a user what their people are watching. Discover already answers "what is the world
watching" (Trending, TMDB's weekly list) and "what does the model think you would like" (My
Recommendations). Popular with Friends is the third answer: the shows the viewer's accepted
connections have been active on lately, ranked by how many of them were. It is the reason to
connect with someone on the app that the feed, a chronological list, never quite gives.

## 2. Scope

**In:**

1. One backend route, `GET /me/friends/popular`, computed live from `app.activity_event` over
   the viewer's accepted connections, honouring both sharing switches, ranked and capped.
2. A fourth Discover tab, *Popular with Friends*, rendering that list through `ShowGrid` with a
   per-card *N friends* fact and two empty states.
3. Glossary terms, ADR-0015, this spec (already merged with the project scaffold).

**Out (noted, not ticketed):**

- A time-windowed count on the show page's friend strip (`ShowFriendActivityStrip` lists names
  with no window). Would share this project's window and privacy rule; its own ticket later.
- Mounting the combined friends feed. `components/friends/ActivityFeed.tsx` and `GET /me/feed`
  exist but no page renders them; unrelated to popularity and belongs in maintenance.
- Making the engagement strip or the friend library honour the sharing switches. They do not
  today; ADR-0015 says any *aggregate* must, and leaves the per-show name lists as they are.
- Names on the card, a "new this week" mark, any weighting or tuning surface, a snapshot job.

## 3. Decisions (the grilling outcome)

| # | Decision | Why |
| -- | -- | -- |
| Q1 | **Rank by distinct connections active on the show**, tie-break by that group's event count | "With friends" is about breadth: three friends on a show beats one friend bingeing it. Immune to a single friend's bulk backfill. |
| Q2 | **All six activity kinds count** — `added_show`, `watched_episode`, `watched_season`, `watched_show`, `rated_show`, `rated_episode` | A small friend graph needs every signal; an add or a rating is the show being on a friend's mind. |
| Q3 | **Window: 14 days**, on `activity_event.created_at` | Catches a weekly show twice and survives a quiet week. TMDB's 7 days is calibrated over millions of users; these graphs are single digits. |
| Q4 | **Home: Discover, fourth tab** | The result is a ranked grid of shows, the same kind of thing as its neighbours; the Friends page is people management. |
| Q5 | **Name: "Popular with Friends"** | "Trending" is TMDB's list — the tab, the route, the table. A Discover tab called "Friends" would collide with the nav item. |
| Q6 | **Both sharing switches honoured** (`activity_feed_enabled`, `hide_from_activity`) | The Privacy page promises it; with a one-friend graph the surface would otherwise name the hidden show. Same predicates as the feed query. |
| Q7 | **Attribution: a friend count only**, no names | A 375px card is ~109px wide and already carries two fact badges and a control. The show page strip answers *who*. |
| Q8 | **Live query at read time**, no job, no table | Per-viewer by construction, so there is no shared snapshot to take; the indexed scan over a handful of friends is trivial. |
| Q9 | **One friend qualifies; cap 24; deterministic order** | A 2-friend floor would leave most beta users with an empty tab. 24 matches Most Anticipated. |
| Q10 | **Tab always visible, two empty states** | No connections: a CTA to connect. Connections but a quiet fortnight: a quiet line. A hidden tab would never be discovered by the users it should recruit. |
| Q11 | **Scope is the Discover tab alone** | The two adjacent findings (§2 *Out*) are recorded, not built here. |

Conventions carried over without discussion, because they are existing rules: consumption
gate (an accepted connection is the only requirement, no verified-email check — CONTEXT.md
*Outreach / consumption*); disabled users excluded through `accepted_friend_ids` (NEU-1162);
`in_my_shows` is a mark, never a filter (NEU-1056 §5); `my_rating` filled, `genres` `[]`,
`network` `null` (the Trending shape); read-time adult and tombstone filter (NEU-1053, NEU-1108);
`Cache-Control: private, no-store` for a per-user body; empty is `200`, never `204`.

## 4. Data

**No new tables, columns or migrations.** The route reads:

| Table | For |
| -- | -- |
| `app.connection` via `connection_service.accepted_friend_ids` | The viewer's accepted, enabled connections. Never the repo function directly — the service is the seam that drops disabled users. |
| `app.activity_event` | The activities: `actor_id`, `verb`, `target_type`, `target_id`, `created_at`. Index `ix_activity_event_actor_created` serves the scan. |
| `app.user.activity_feed_enabled` | The global sharing switch, joined on the actor. |
| `app.user_show_watch.hide_from_activity` | The per-show sharing switch, joined on (actor, resolved show). |
| `catalog.episode.show_id` | Resolving an episode-targeted activity to its show. |
| `catalog.show` | The `ShowSummary` fields and the adult/tombstone filter. |

**Resolving an activity to a show** is the same rule the feed query uses: `target_type =
'episode'` → `catalog.episode.show_id`; otherwise `target_id` is the show id (`added_show`,
`watched_season`, `watched_show`, `rated_show` all target the show). An episode activity whose
episode no longer exists resolves to nothing and is dropped.

## 5. Backend behaviour

### 5.1 The query

One statement, in `app/repos/activity_repo.py` beside the feed query, because it is the same
table under the same two privacy predicates and the same show-resolution rule. Shape:

```sql
WITH resolved AS (
    SELECT e.actor_id, e.created_at,
           CASE WHEN e.target_type = 'episode' THEN ep.show_id
                ELSE e.target_id::int END AS show_id
    FROM app.activity_event e
    JOIN app.user u ON u.id = e.actor_id AND u.activity_feed_enabled = TRUE
    LEFT JOIN catalog.episode ep ON e.target_type = 'episode' AND ep.id = e.target_id
    WHERE e.actor_id = ANY(:friend_ids)
      AND e.created_at >= now() - make_interval(days => :window_days)
),
visible AS (
    SELECT r.* FROM resolved r
    LEFT JOIN app.user_show_watch usw ON usw.user_id = r.actor_id AND usw.show_id = r.show_id
    WHERE r.show_id IS NOT NULL AND usw.hide_from_activity IS NOT TRUE
)
SELECT v.show_id,
       COUNT(DISTINCT v.actor_id) AS friend_count,
       COUNT(*)                   AS activity_count,
       MAX(v.created_at)          AS last_activity_at
FROM visible v
JOIN catalog.show s ON s.id = v.show_id            -- plus the read-time adult/tombstone filter
GROUP BY v.show_id
ORDER BY friend_count DESC, activity_count DESC, last_activity_at DESC, v.show_id
LIMIT :limit;
```

Rules the query must keep:

- **The two privacy predicates and the show-resolution `CASE` are the feed's.** If the feed
  query's are ever changed, this one changes with them; a test in each file asserts the switch
  behaviour so drift fails loudly. Factor a shared SQL fragment only if it falls out cleanly —
  two copies with two tests is acceptable, one predicate silently missing is not.
- **The viewer's own activity is never counted.** `friend_ids` comes from
  `accepted_friend_ids(viewer)`, which does not contain the viewer.
- **`friend_ids` empty short-circuits** to no query and an empty list — `= ANY('{}')` is
  correct but pointless.
- **The window is one constant**, `POPULAR_WINDOW_DAYS = 14`, and the cap one constant,
  `POPULAR_LIMIT = 24`, both in the repo module, neither a query parameter.
- **The ordering is total** (the show id is the last key) so two reads of an unchanged ledger
  return the same grid.
- **The adult/tombstone filter is the read-time one every browse list applies**, not a new
  copy; reuse whatever predicate helper `get_trending_snapshot` / `list_similar` share.

### 5.2 The route

```
GET /me/friends/popular
```

- **Auth:** `get_current_user`. No verified-email gate (consumption). No CSRF (a GET).
- **Cache:** `Cache-Control: private, no-store` — the body carries `in_my_shows` and
  `my_rating`, which mutate through `/me/*` with no browser-cache invalidation (CLAUDE.md, the
  browse cache rule).
- **Router:** `routers/friend_engagement.py`, the friend-scoped reader module that already goes
  through `accepted_friend_ids`. Not `browse.py` (this is not a catalog claim) and not `me.py`
  (which owns the viewer's *own* data).
- **Query parameters:** none. No `limit`, no `window`, no sort.

**Response, `200`:**

```json
{
  "window_days": 14,
  "connection_count": 4,
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
      "in_my_shows": true,
      "friend_count": 3
    }
  ]
}
```

- `shows[]` is **`MarkedShowOut` flattened plus `friend_count`** — `PopularShowOut(MarkedShowOut)`
  in `catalog/schemas.py`, a sibling of `TrendingShowOut` for its reason: `ShowGrid` / `ShowCard`
  take a `ShowSummary`, and a wrapper would cost the SPA something for two scalars.
- `friend_count` is the number of **distinct** accepted connections with visible activity on
  the show in the window; it is ≥ 1 by construction. The activity count and the last-activity
  time are ordering keys and are **not exposed** — a number the client can render is a number
  it will, and neither means anything to a user.
- `connection_count` is the size of `accepted_friend_ids(viewer)` — enabled, accepted
  connections — and is what lets the SPA pick between its two empty states without a second
  request or any rule of its own. It is present on every response, including a non-empty one.
- `window_days` is informational copy fuel ("in the last two weeks"); the SPA never computes
  with it.
- **`shows` is in server rank order and the client never re-sorts it.** The rank is not
  exposed.
- `in_my_shows` from `show_membership_repo.tracked_show_ids`; `my_rating` from
  `browse_queries.hydrate_my_ratings`; summaries from `build_show_summary(show, genre_names=[],
  network=None, my_rating=...)`. Three hydration queries after the one ranking query.

**Degradation.** Empty is `200` with `shows: []` and the true `connection_count`, never `204`
and never `404`. Two situations produce it and the SPA is *meant* to tell them apart, which is
the one way this route differs from `/trending`:

| Situation | Body |
| -- | -- |
| No accepted, enabled connections | `{"window_days": 14, "connection_count": 0, "shows": []}` |
| Connections, but no visible activity in the window | `{"window_days": 14, "connection_count": N, "shows": []}` with N ≥ 1 |

A connection whose every activity is hidden by a switch looks, on this route, like a quiet
one — it still counts toward `connection_count`. That is deliberate: the count is "how many
people could contribute", and reporting it lower would itself leak that someone hid something.

### 5.3 Where it lives

| Piece | File |
| -- | -- |
| Query, constants | `src/tvbf/app/repos/activity_repo.py` — `popular_shows_for_friends`, `POPULAR_WINDOW_DAYS`, `POPULAR_LIMIT` |
| Route | `src/tvbf/routers/friend_engagement.py` — `get_popular_with_friends_route` |
| Shapes | `src/tvbf/catalog/schemas.py` — `PopularShowOut`, `PopularWithFriendsOut` |
| Friends seam | `src/tvbf/app/services/connection_service.py` — `accepted_friend_ids` (existing) |
| Contract tests | `tests/integration/routers/test_popular_with_friends.py` |
| Docs to update | `.claude/CLAUDE.md` endpoint index (Friend engagement line — it is stale at two routes; make it five), `.claude/docs/architecture-endpoints.md` |

## 6. Frontend behaviour

### 6.1 The tab

- `DISCOVER_TABS` gains `"popular-with-friends"`, placed **after `trending`** and before
  `most-anticipated`: the world's list, then your people's, then the calendar. Label
  **"Popular with Friends"**.
- The tab is **always present**, unlike My Recommendations — its empty states are the point
  (§6.3). The persisted-tab healing in `DiscoverPage` needs no change; the new value is simply
  valid.
- Four triggers may not fit a 375px `TabsList`. The rule is **measure, then let the list scroll
  horizontally** if it does not; do not abbreviate the label per breakpoint, and do not drop a
  tab.
- Component `components/discover/PopularWithFriends.tsx`; hook `usePopularWithFriends` in
  `api/me.ts` (the path is `/me/...`), `queryKey: ["popular-with-friends"]`, `staleTime: 0` on
  `useTrending`'s reasoning (`no-store` body carrying per-user, user-mutable fields); types
  `PopularShow extends MarkedShow { friend_count: number }` and `PopularWithFriends` in
  `api/types.ts`.
- The grid is `<ShowGrid shows={data.shows} />` — no `addable`, no `dismissible`, no sort or
  filter. A tracked show is marked, never dropped.

### 6.2 The friend count on the card

- `friend_count` is a **fact, not a control** (`ShowPoster`'s rule, NEU-1183 §3.4). Both fact
  corners are assigned (library mark top-left, own rating top-right), so it does **not** take
  a poster corner: it renders **inline beneath the title**, where an aggregate rating already
  goes (§3.5). Copy: `1 friend` / `N friends`.
- It reaches the card the way `in_my_shows` does: `ShowGrid`'s `shows` type widens to an
  optional `friend_count`, the grid threads the flat value, and `ShowCard` renders the line
  only when the value is present. **`ShowCard.test.tsx` asserts it absent by default**, the
  containment rule every surface-specific affordance follows there.
- Nothing about the card is otherwise different: same poster, same badges, same My Shows
  button.

### 6.3 Empty states

`ShowGrid` renders "No shows match your filters." for an empty list, which is wrong copy
here, so the surface never hands it an empty array. It branches on the envelope:

| `connection_count` | `shows` | Render |
| -- | -- | -- |
| 0 | `[]` | "Connect with friends to see what they're watching." with a link to `/friends`. |
| ≥ 1 | `[]` | "Nothing from your friends in the last two weeks." No link. |
| any | non-empty | The grid. |

Loading: nothing (no spinner), matching the other tabs. Error: nothing, matching Trending —
the tab stays, the pane is empty. A `sr-only` `<h2>` carries the title for the outline.

## 7. Cross-cutting rules

- **The viewer sees a count, never a name, on this surface.** Names live on the show page's
  strip, which the card links to.
- **Every predicate that hides an activity from the feed hides it here.** No surface-specific
  exception, in either direction.
- **No new job, table, column or setting.** If latency ever argues for a snapshot, that is a
  new decision against ADR-0015's "cheap" claim, with a measurement attached.
- **The SPA computes nothing about ranking, windows or thresholds.** It renders the list in
  the order served and branches on `connection_count` only.
- **Blocked, pending and disabled connections contribute nothing** — inherited from
  `accepted_friend_ids`, asserted by a contract test rather than re-implemented.

## 8. Testing

Backend (`tests/integration/routers/test_popular_with_friends.py`, plus repo-level cases):

- Ranking: two friends on show A, one friend with three activities on show B → A first.
- Tie-break: equal friend counts fall to activity count, then recency, then id; a re-read
  returns the same order.
- Window: an activity 15 days old is out, 13 days in; a re-emitted (undone, redone) activity
  counts at its new timestamp.
- Every verb counts once per friend per show: six different verbs from one friend on one
  show → `friend_count: 1`.
- Both switches: `activity_feed_enabled = FALSE` removes the friend's contribution entirely;
  `hide_from_activity = TRUE` removes only that show's — and neither changes
  `connection_count`.
- Graph: pending, blocked and disabled users contribute nothing; the viewer's own activity
  contributes nothing.
- Hydration: `in_my_shows` true for a tracked show that still appears; `my_rating` filled.
- Read-time filter: an adult or tombstoned show is absent.
- Degradation: the two empty bodies of §5.2; header is `private, no-store`; unauthenticated
  is `401`.
- Query count: the route issues a fixed number of statements whatever the list length.

Frontend:

- Tab present with no data, with data, and with each empty state; the CTA links to `/friends`.
- Card shows `1 friend` / `N friends` when the value is present; `ShowCard.test.tsx` asserts
  it absent by default.
- The list renders in the order received (no client sort).

## 9. Milestone map

| Milestone | Delivers |
| -- | -- |
| 1. The contract | §5 — query, route, shapes, tests, endpoint docs. Backend only. |
| 2. The tab | §6 — types, hook, tab, empty states, card fact. Frontend only; consumes §5.2 verbatim. |
