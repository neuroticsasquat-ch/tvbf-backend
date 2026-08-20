# NEU-1062 — Segregate special episodes

**Ticket:** [NEU-1062](https://linear.app/neuroticsasquatch/issue/NEU-1062/segregate-special-episodes)
**Repo:** `tvbf-backend` (backend only)
**Frontend half:** [NEU-1129](https://linear.app/neuroticsasquatch/issue/NEU-1129/frontend-label-the-specials-season-specials-rather-than-season-0) — labels the season "Specials" rather than "Season 0". Independent, not a blocker.

## Why

A user who has watched every regular episode of a show and none of its specials
sees something less than 100%. That is the whole ticket. Everything below is in
service of it.

The original stub asked for two things — *"move special episodes out of seasons
into their own virtual season listed last"* and *"do not include special episodes
in show watch progress"*. The TMDB migration did half of the first one and made
the other half impossible; the second one is the substance.

## What a special is, and why it is two things

Measured against production, 2026-08-12:

| | episodes | shows | watch records |
| -- | --: | --: | --: |
| Season 0 — TMDB-native special | 106,584 | 12,151 | 0 |
| Negative `episode_number` inside a real season — copied TV Maze special | 20,973 | 5,051 | **156** |
| Regular | 7,201,720 | 208,899 | 6,982 |

TMDB models a special as **season 0 with a real episode number** (audit D2), so
the first row already sits in its own season. It is simply in the wrong *place*:
season 0 sorts ahead of season 1, so those 12,151 shows list their specials
first.

The second row will never join it. NEU-1042 numbered TV Maze's null-numbered
specials `-1, -2, …` **within their real season** (see `catalog/episodes.py` for
why negatives rather than a contiguous tail), and NEU-1126 kept exactly the ones
with no TMDB counterpart — locally-authored rows (ADR-0008) that no ingest or
delta will ever revisit.

### Decision: a predicate, not a migration

Re-homing those 20,973 rows into a season 0 would mean minting a
`catalog.season` per affected show and moving 156 rows that
`app.user_episode_watch` points at, to buy tidiness in a representation
`catalog/episodes.py` deliberately chose to make *look* invented. Rejected.

Instead, "is a special" becomes a predicate defined **once**, beside the ordering
rule that already handles half of it:

```python
# catalog/episodes.py, beside EPISODE_ORDER and public_number
IS_SPECIAL = or_(Episode.season_number == 0, Episode.episode_number < 0)
IS_COPIED_SPECIAL = Episode.episode_number < 0
```

`catalog/episodes.py` is the only module that decides what a special is.
`app/repos/` importing from `catalog/` is established — `season_repo` already
imports `catalog.seasons`.

Verified: no `season_number` anywhere in `catalog` is negative (`min` is 0 on
both `season` and `episode`), so `season_number == 0` is a total, unambiguous
test for the TMDB-native case.

## 1. Season 0 sorts last

`EPISODE_ORDER` already puts a copied special last *within* its season. Season 0
needs the same treatment one grain up — last among seasons rather than first:

```python
_SPECIALS_SEASON_LAST = (Episode.season_number == 0).asc()
EPISODE_ORDER = (_SPECIALS_SEASON_LAST, season_number.asc(), _SYNTHETIC_LAST, episode_number.asc())
```

and the equivalent in `browse_queries.get_show_seasons`, currently
`ORDER BY season_number, id`.

A show then reads: Season 1 … Season 5, then Specials — with any copied specials
still trailing their own season, exactly where they have always been.

## 2. Specials do not count toward progress

Both halves of the fraction must exclude them, or a user who *has* watched
specials exceeds 100%.

### The three-way split

The call sites do not divide two ways. Each one's treatment is a separate
decision, and half of them must **not** change:

| Treatment | Sites |
| -- | -- |
| Exclude **both** (`~IS_SPECIAL`) | `episode_repo.count_per_show`, `count_aired_per_show`, `latest_aired_per_show`, `earliest_aired_unwatched_per_show`, `earliest_future_per_show`, `next_unwatched`, `list_aired_episode_ids_for_show`; `episode_watch_repo.count_watched_per_show` |
| Exclude **copied only** (`~IS_COPIED_SPECIAL`) | `episode_repo.aired_count_per_season`, `list_episode_ids_for_season`; `episode_watch_repo.watched_count_per_season` |
| Exclude **nothing** | `episode_repo.get_by_id`, `episode_repo.list_episode_ids_for_show`, `episode_watch_repo.list_episode_ids_for_show` |

**Why per-season excludes only the copied ones.** Season 0's own row should
report its own contents — `3/9 specials watched` is useful and true — so the
per-season queries strip only a copied special inflating the *real* season it
hangs inside. Show-level math strips both.

**Why three sites change nothing.** `get_by_id` serves a special's own episode
page. `episode_watch_repo.list_episode_ids_for_show` backs
`GET /me/shows/{id}/episodes/watched`, which the show page uses to render ticks —
a watched special must still show as watched. `episode_repo.list_episode_ids_for_show`
backs *un*-marking a whole show, which must remove every watch row or it orphans
the special ones.

### Decision: explicit predicates per site, plus a ledger test

A shared `regular_episodes()` base selectable was rejected. No default is right
often enough — it would need overriding at 5 of 12 sites, and a forgotten
override fails **silently in the dangerous direction**: unmark-show leaving
orphan rows, or an episode page 404ing a special. Explicit-at-each-site fails
loudly instead, because the site that forgot has no test.

Paired variants (`count_per_show` / `count_regular_per_show`) were also
rejected: double the repo surface to encode one bit, and the caller still has to
choose correctly.

**The ledger test is what catches site #13.** One test enumerating every
episode-reading query and asserting its treatment against a fixture show
carrying a regular season, a season 0, and a copied negative special. A query
added later with no row in that ledger is the signal.

## 3. Watch Next and Upcoming skip specials

`earliest_aired_unwatched_per_show` and `earliest_future_per_show` each return
one episode per show. An unwatched special must not be offered as the next thing
to watch, and an upcoming special must not be what a show is "waiting for".

`list_upcoming_seasons` reads `season_repo.unaired_for_shows` — a season 0 whose
specials have not aired must not surface as an upcoming *season*. Note that
function's existing dedupe-before-filter rule stays exactly as it is; this adds a
filter, it does not reorder the two.

## 4. Bulk marking skips specials

`POST /me/shows/{id}/watched` marks every aired episode via
`list_aired_episode_ids_for_show`, specials included. It marks only regular aired
episodes after this, so "mark all watched" leaves the show reading 100% rather
than ticking rows that count toward nothing.

`POST /me/shows/{id}/season/{n}/watched` on **season 0** still marks its
episodes — marking the Specials season is a deliberate act and stays available.
Within a *regular* season it skips the copied negatives.

Un-marking is unchanged at every grain: it only ever removes rows that exist.

## 5. The friend feed is left alone

A `watched_episode` activity event still fires for a special, and still appears
in the feed.

Once bulk-marking skips specials, **every remaining special watch is a deliberate
act** — someone ticked that box on purpose. Suppressing it would misreport what
they did. The feed answers *"what did my friends do"*, which is a different
question from *"how far through is this show"*, and only the second is what
specials distort. Filtering it would also mean a friend watching a Christmas
special generates nothing at all, which reads as a bug.

**One pre-existing bug to fix while in there.** `feed_service.py:99` renders
`number=public_number(episode_obj) or 0`, so a copied special — whose
`public_number` is correctly `None` — appears in the feed as **episode 0**. The
`or 0` predates this ticket and is wrong independently; carry the null through.

## 6. A show with no regular episodes

357 shows are 100% specials. With specials excluded their denominator is 0.
**None is tracked or watched by any user today**, so this is untidy for nobody —
but the rule is explicit rather than emergent: such a show has *no* progress (no
bar, never `finished`), which is what the existing `aired > 0` guards already
produce. Asserted in a test so it stays deliberate.

## Verification

**No new tooling.** `jobs/reconcile.py`'s capture/verify shape exists because the
migration moved millions of rows across a schema boundary and needed a
machine-checkable gate. This moves 18 user-show pairs' worth of arithmetic, and
the expected answer is already computed below.

Run this before and after deploy; the `pct_after` column is the assertion.

```sql
WITH aired AS (
  SELECT show_id,
         count(*) FILTER (WHERE NOT (season_number = 0 OR episode_number < 0)) AS reg_aired,
         count(*) FILTER (WHERE     (season_number = 0 OR episode_number < 0)) AS spc_aired
    FROM catalog.episode
   WHERE air_date IS NOT NULL AND air_date <= current_date
   GROUP BY show_id),
w AS (
  SELECT uw.user_id, e.show_id, count(*) AS watched,
         count(*) FILTER (WHERE e.season_number = 0 OR e.episode_number < 0) AS spc_watched
    FROM app.user_episode_watch uw JOIN catalog.episode e ON e.id = uw.episode_id
   GROUP BY 1, 2)
SELECT s.name,
       round(100.0 * w.watched / nullif(a.reg_aired + a.spc_aired, 0))            AS pct_before,
       round(100.0 * (w.watched - w.spc_watched) / nullif(a.reg_aired, 0))        AS pct_after
  FROM w JOIN aired a USING (show_id) JOIN catalog.show s ON s.id = w.show_id
 WHERE w.spc_watched > 0
 ORDER BY 2;
```

Expected, measured 2026-08-12 — **18 pairs, none decreasing, nine landing on
exactly 100%**:

| Show | before | after |
| -- | --: | --: |
| Mr. Robot | 49% | **100%** |
| Girls | 62% | **100%** |
| Lost | 78% | **100%** |
| 30 Rock | 82% | **100%** |
| Saturday Night Live | 83% | **100%** |
| Breaking Bad | 90% | **100%** |
| Better Call Saul | 92% | **100%** |
| Parks and Recreation | 93% | **100%** |
| Brooklyn Nine-Nine | 95% | **100%** |
| Friends | 99% | **100%** |
| Sex and the City | 96% | **100%** |
| The Boys | 29% | 83% |
| The Expanse | 23% | 37% |
| The Bear (×2), Ozark, Kimmy Schmidt, And Just Like That… | unchanged | unchanged |

**Zero user-show pairs are watched *only* through specials**, so no show drops
out of anyone's Watched library (`list_watched` skips `watched == 0`).

### The counterexample belongs in a test, not a comment

A percentage *can* fall. If a user has watched 2 of 10 regular aired episodes and
all 5 aired specials, they read 7/15 = 47% today and 2/10 = 20% after. It does
not occur in production data — Saturday Night Live comes closest with 89 specials
watched and still rises, because 1,008 of 1,009 regulars are watched too — but it
is the shape that would surprise someone later. It goes in the unit suite, where
a reader will meet it, rather than in a runbook nobody re-reads.

## Out of scope

- **The "Specials" label** — NEU-1129, frontend. `SeasonOut.name` already carries
  "Specials" for 12,633 of 12,638 season-0 rows; the SPA hardcodes
  `Season {s.number}`.
- **Any data migration.** Nothing in `app` or `catalog` is written or moved.
- **Rating math.** `user_episode_rating` on a special is untouched; no aggregate
  reads it today.
- **`catalog.show.number_of_episodes`** — TMDB's own field, which already
  excludes season 0. Not used by progress math and not reconciled here.

## Acceptance criteria

- A user who has watched every regular aired episode and no specials sees
  **100%**, and `finished` for an ended show.
- A user who has watched some specials does not exceed 100%, and their watched
  count excludes those specials.
- Season 0 is listed **last** in a show's season list and episode ordering; a
  copied special still sorts last within its own season, unchanged.
- Watch Next never offers a special; Upcoming never reports a special or a
  specials-only season.
- Bulk-marking a show watched marks no specials; marking season 0 explicitly
  still works; un-marking still removes special watches.
- Per-season progress for season 0 reports season 0's own episodes; a regular
  season's progress excludes the copied specials hanging inside it.
- A show with no regular episodes reports no progress rather than 0% or a
  division error.
- A special's own episode page still renders, and a watched special still shows
  as watched on the show page.
- A special watch still appears in the friend feed, with a **null** episode
  number rather than 0.
- The ledger test enumerates every episode-reading query and its treatment.
- `catalog/episodes.py` is the only place that decides what a special is.

## Notes for the implementer

- No frontend change is needed for the math: `WatchProgressBar` computes
  `Math.round((watched / aired) * 100)` from backend-supplied
  `watched_episode_count` / `aired_episode_count`, and `MyShowCard` /
  `LibraryWatchedList` read the same fields. One repo, one PR.
- Field *names* in the API do not change; their *meaning* narrows to "regular
  episodes". That is the point of the ticket, not a contract break to avoid.
- No ADR. The decisions here are local to one module and fully recorded in this
  spec and in `catalog/episodes.py`'s docstring; ADR-0008 already covers the
  locally-authored-row premise the copied specials rest on.
