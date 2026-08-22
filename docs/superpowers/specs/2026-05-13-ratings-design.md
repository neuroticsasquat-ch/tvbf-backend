# Ratings — Design

**Date:** 2026-05-13
**Status:** Proposed
**Owner:** Tom
**Linear:** NEU-60 (project: Ratings & Reviews)

## Problem

Users have no way to record their own opinion of the shows and episodes they watch on TV Binge Friend, and no way to see what their connections think. The TV Maze catalog mirror already carries an aggregate `rating.average` on shows and episodes, but we don't currently store or surface it. Once we have per-user ratings + the existing connection model, the show and episode pages can become much more useful: a TV Maze aggregate sits next to your friends' individual ratings, and your own rating becomes a first-class sort / filter on My Shows.

## Goal

Ship a self-contained ratings feature: users can rate shows and episodes on a 1–5 half-star scale, view TV Maze's aggregate alongside individual ratings from their accepted connections on show and episode pages, and sort/filter My Shows by their own rating.

## Non-goals

- **Reviews / free-text commentary.** A separate Linear project will handle reviews later.
- **Season-level ratings.** Granularity is show + episode only.
- **Multi-axis ratings** (separate writing / acting / direction scores). Single overall rating.
- **Aggregating tvbf user ratings into a public "site average".** We display TV Maze's aggregate, not our own. Avoids the cold-start problem and matches v1's tiny user base.
- **A dedicated `/me/ratings` page.** Useful eventually, deferred to v2.
- **Watch-state coupling.** Rating is decoupled from watch tracking; rating something does not mark it watched, and rating does not require having marked anything watched.
- **Spoiler hiding on episode pages.** Connection ratings on episodes are always shown (user explicitly chose "show always" — adding a setting later is cheap if it comes up).

## Architecture

### New tables (`app` schema)

Two tables mirroring the existing `user_show_watch` / `user_episode_watch` split:

```sql
CREATE TABLE app.user_show_rating (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES app.user(id) ON DELETE CASCADE,
    show_id     BIGINT NOT NULL REFERENCES tvmaze.show(id) ON DELETE CASCADE,
    stars       NUMERIC(2,1) NOT NULL,
    rated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_user_show_rating_stars CHECK (stars IN (0.5,1.0,1.5,2.0,2.5,3.0,3.5,4.0,4.5,5.0)),
    UNIQUE (user_id, show_id)
);
CREATE INDEX ix_user_show_rating_user_id  ON app.user_show_rating(user_id);
CREATE INDEX ix_user_show_rating_show_id  ON app.user_show_rating(show_id);

CREATE TABLE app.user_episode_rating (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES app.user(id) ON DELETE CASCADE,
    episode_id  BIGINT NOT NULL REFERENCES tvmaze.episode(id) ON DELETE CASCADE,
    stars       NUMERIC(2,1) NOT NULL,
    rated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_user_episode_rating_stars CHECK (stars IN (0.5,1.0,1.5,2.0,2.5,3.0,3.5,4.0,4.5,5.0)),
    UNIQUE (user_id, episode_id)
);
CREATE INDEX ix_user_episode_rating_user_id      ON app.user_episode_rating(user_id);
CREATE INDEX ix_user_episode_rating_episode_id   ON app.user_episode_rating(episode_id);
```

Cross-schema FKs follow the existing convention (`user_show_watch.show_id`, etc.). `ON DELETE CASCADE` on both sides — deleting a user removes their ratings, and the very rare TV Maze show/episode merge does the same.

### Catalog mirror columns

```sql
ALTER TABLE tvmaze.show    ADD COLUMN rating_average NUMERIC(3,1);
ALTER TABLE tvmaze.show    ADD COLUMN ratings_synced_at TIMESTAMPTZ;
ALTER TABLE tvmaze.episode ADD COLUMN rating_average NUMERIC(3,1);
```

- `rating_average` stores TV Maze's raw 0.0–10.0 value (single decimal, nullable — many older shows have no rating).
- `ratings_synced_at` exists only on `tvmaze.show` and drives the one-shot backfill, mirroring the `akas_synced_at` pattern: `NULL` means "not yet synced." Once a show is processed, the column is stamped and the row is skipped on subsequent backfill iterations. Episode ratings are synced as a side-effect of fetching the show (TV Maze returns them embedded), so no per-episode flag is needed.

### TV Maze payload parsing

`tvmaze.api_payloads.TVMazeShow` and `TVMazeEpisode` already expose a `rating: Rating | None` nested object where TV Maze provides `{"average": float | null}`. Add a small extractor (or `model_validator`) that flattens it to `rating_average: float | None` on the upsert path. No new `OptionalDate` / `OptionalTime` analogs needed; the field is already a plain optional float.

The daily-delta cycle picks up rating changes automatically — once the field flows through the upsert, no separate cron is required.

### Backfill

New admin orchestrator at `src/tvbf/tvmaze/ratings_backfill.py`, modeled after `akas_backfill.py`:

- `POST /admin/backfill-ratings` (bearer token) — spawns `asyncio.create_task`, returns `202 + run_id`.
- `GET /admin/backfill-ratings/{run_id}` — status polling.
- Iterates `tvmaze.show.id WHERE ratings_synced_at IS NULL` in `tvmaze_id` order so it's resumable.
- For each show, fetches `/shows/{id}?embed=episodes` via the existing rate-limited client (single call covers show + all episode ratings).
- Upserts `tvmaze.show.rating_average`, sets `ratings_synced_at = now()`, and upserts every episode's `rating_average` in the batched 1000-per-query path.
- Per-show failures non-fatal; `INGEST_CONSECUTIVE_FAILURE_THRESHOLD` applies as it does for ingest/AKAs.
- Reuses the `ingest_run` table (`kind = 'ratings_backfill'`) for run state.

Task target: `task backfill:ratings` / `task backfill:ratings:status -- <uuid>`, matching `akas:backfill`.

### Display normalization

TV Maze stores 0–10 with one decimal; user ratings are 0.5–5.0 with half-step granularity. Display is normalized to a 1–5 scale with 0.1 precision so the two coexist on the same UI bar.

**The backend ships raw values.** TV Maze aggregate goes over the wire as 0–10. Friend ratings go over the wire in their native 0.5–5.0 scale. The frontend owns the display normalization in a single helper:

```ts
// src/lib/rating.ts
export const tvmazeToFiveStar = (v: number | null | undefined): number | null =>
  v == null ? null : Math.round((v / 2) * 10) / 10;
```

Friend aggregate avg (already in 0.5–5.0) is rounded to 0.1 server-side and shipped as-is.

### Backend API

**User ratings** (auth + CSRF on mutations):

| Method | Path | Body | Response |
|--------|------|------|----------|
| `PUT`    | `/me/shows/{show_id}/rating`        | `{stars: float}` | `{show_id, stars, rated_at}` |
| `DELETE` | `/me/shows/{show_id}/rating`        | —                | 204 |
| `PUT`    | `/me/episodes/{episode_id}/rating`  | `{stars: float}` | `{episode_id, stars, rated_at}` |
| `DELETE` | `/me/episodes/{episode_id}/rating`  | —                | 204 |

Mutations are upserts on the `UNIQUE (user_id, target_id)` constraint. Body validation: `stars` must be in `{0.5, 1.0, ..., 5.0}`; anything else → 422. 404 if the show/episode does not exist.

**Connection ratings** (auth, no CSRF — pure read):

| Method | Path | Response |
|--------|------|----------|
| `GET` | `/shows/{show_id}/friends/ratings`        | `{avg, count, items: [{user_id, display_name, stars, rated_at}]}` |
| `GET` | `/episodes/{episode_id}/friends/ratings`  | same shape |

Both endpoints reuse the existing `_accepted_friend_ids(db, user.id)` helper from `friend_engagement.py` and restrict the rating query to that ID set. `items` is sorted by `rated_at DESC` (newest first). `avg` is `NULL` when `count = 0`; otherwise rounded to one decimal.

**Browse / Me response model changes:**

- `ShowSummary`, `ShowDetail`, `EpisodeDetail` gain `rating_average: float | None` (raw TV Maze 0–10).
- `MyShowsItem` gains `my_rating: float | None`.
- `MyShowsSort` literal gains `my_rating_desc` and `my_rating_asc`. Comparator: rated rows first (sorted by `stars`), unrated last. Tie-broken by `name` ascending.
- `GET /me/shows` gains an optional `rated_only: bool` query param. When `true`, returns only My Shows where the caller has a rating.

### Browse list "my rating" badge — the user-specific browse decision

Currently `GET /shows` and `GET /shows/{id}` are user-gated (require auth) but return identical payloads to every authed caller, so they ship `Cache-Control: public, max-age=300`. Surfacing the caller's own rating on browse cards makes the payload user-specific, which is incompatible with a `public` cache.

**Resolution:** add `my_rating: float | None` to `ShowSummary` and `ShowDetail`, and downgrade the browse cache header on those routes from `public, max-age=300` to `private, max-age=60`. The browse query layer in `browse_queries.list_shows` gains a `viewer_id` parameter and left-joins `user_show_rating` to attach the value per row.

Browse-card user-specificity is opt-in per route — `/genres` and `/networks` keep their public 300s cache; only the show endpoints are downgraded. Episode browse responses already nest under show endpoints so they inherit naturally.

### Frontend changes

**New components / lib:**

- `src/lib/rating.ts` — `tvmazeToFiveStar` helper + a `formatStars(value)` shared formatter.
- `src/components/StarRatingInput.tsx` — half-step interactive rater (mouse + keyboard). Calls `useShowRating` / `useEpisodeRating` mutate hooks. Optimistic update + revert on error.
- `src/components/StarRatingDisplay.tsx` — read-only 5-star bar with 0.1 precision and an aria-label of the numeric value.
- `src/components/FriendRatingsList.tsx` — renders aggregate (`avg · count`) above a list of `{avatar, display name (links to /u/{user_id}), stars, relative time}`. Empty state when no friends have rated.
- `src/api/me.ts` — adds `useShowRating`, `useEpisodeRating`, `useFriendShowRatings`, `useFriendEpisodeRatings` (React Query).
- `src/components/ShowCard.tsx` & `MyShowCard.tsx` — accept optional `myRating` prop, render a small star badge top-right when present.

**Page touches:**

- `ShowDetailPage`: TV Maze aggregate (normalized) next to the show title, a `StarRatingInput` row below, and a `FriendRatingsList` section.
- `EpisodePage`: same structure for episode rating + `/episodes/{id}/friends/ratings`.
- `MyShowsPage`:
  - Two new sort options ("My rating, high → low", "My rating, low → high") wired through `home/myShowsSort.ts` (extract a comparator + `SORTS` entry).
  - A "Rated only" filter toggle that pipes to the `rated_only` query param.
  - Each `MyShowCard` gets the badge when `myRating` is set.

The TV Maze aggregate on browse cards is opt-in: small numeric like `★ 4.1` next to the show title, only when `rating_average` is non-null.

### Caching matrix

| Route | Header |
|-------|--------|
| `GET /shows`, `GET /shows/{id}` | `Cache-Control: private, max-age=60` *(downgraded — adds `my_rating`)* |
| `GET /shows/{id}/seasons`, `/shows/{id}/episodes`, `/episodes/{id}` | `Cache-Control: private, max-age=60` *(consistency — they nest under shows and gain `rating_average`)* |
| `GET /genres`, `/networks` | `Cache-Control: public, max-age=300` *(unchanged)* |
| All `/me/*` rating endpoints | no-store *(default)* |
| `GET /shows|episodes/{id}/friends/ratings` | no-store *(per-user friend graph)* |

## Testing strategy

**Backend unit tests:**

- Rating validators (accept canonical half-step values, reject 1.3 / 0.0 / 6.0 / negatives / null).
- `tvmazeToFiveStar` equivalent in Python only if any backend caller needs it (probably not — kept FE-side).
- `MyShowsSort` comparator: rated-rows-first ordering for both directions.

**Backend integration tests (DB-backed, `ASGITransport`):**

- `PUT /me/shows/{id}/rating` upserts, allows updating, 422 on out-of-range, 404 on unknown show.
- `DELETE` removes the row, idempotent (second DELETE returns 204 with no error).
- Episode-rating mirror set.
- `GET /shows/{id}/friends/ratings` returns only accepted-connection ratings, excludes pending / blocked, sorted newest first, aggregate avg correct, count correct, empty state shape.
- Same for episode endpoint.
- `/me/shows?rated_only=true` filters correctly; `sort=my_rating_desc` orders correctly with mixed rated/unrated rows.
- `/shows` and `/shows/{id}` include `rating_average` (TV Maze) and `my_rating` (caller).
- Cache-Control header asserted on both browse routes.
- Backfill orchestrator: empty catalog, partial-progress resume, single-show fetch failure increments `shows_failed` without aborting, consecutive-failure threshold aborts the run.

**Frontend tests (vitest + MSW):**

- `StarRatingInput`: keyboard left/right at half-step, click on half / whole positions, clearing via repeat click on current value, optimistic update + revert on 4xx.
- `FriendRatingsList`: renders aggregate + items, empty state, name links route to `/u/{user_id}`.
- `myShowsSort.ts` comparator with mixed nullable inputs.
- "Rated only" filter toggle on `MyShowsPage`.
- `tvmazeToFiveStar` helper edge cases (null, 0, 10, 4.7).

## Migration / rollout

1. Alembic migration: add `tvmaze.show.rating_average`, `tvmaze.show.ratings_synced_at`, `tvmaze.episode.rating_average`, `app.user_show_rating`, `app.user_episode_rating`.
2. Deploy backend with daily-delta picking up new ratings going forward.
3. Run `task backfill:ratings` to populate existing rows (resumable; expected ~12–16h fresh, like AKAs).
4. Deploy frontend.

No data loss risk: the new tables and columns are purely additive. Rolling back means dropping them; nothing references them from elsewhere.

## Open questions resolved during brainstorm

- **Granularity:** show + episode (no seasons).
- **Scale:** 1–5 half-stars (10 buckets total, 0.5 minimum).
- **Reviews:** deferred to a separate project.
- **Aggregation:** display TV Maze's aggregate, not a tvbf aggregate.
- **Privacy:** connection ratings visible only to accepted connections; non-connections see only the TV Maze aggregate.
- **Spoilers on episode pages:** connection ratings always shown — no hiding behavior or setting in v1.
- **Coupling to watch state:** decoupled. Rating does not require watched; rating does not mark watched.
- **Mutability:** ratings are upsert; DELETE clears.
- **Friend ratings sort:** newest first.
- **Profile links from friend list:** yes, link to `/u/{user_id}`.
- **My-rating badge on browse cards:** ship it; downgrade `/shows` cache to `private, max-age=60`.
