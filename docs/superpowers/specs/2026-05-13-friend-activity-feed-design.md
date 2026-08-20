# Friend Activity Feed — Design

**Date:** 2026-05-13
**Status:** Proposed
**Owner:** Tom
**Linear:** NEU-56 (project: Friend Activity Feed)

## Problem

TV Binge Friend has a friend graph (accepted/pending/blocked connections, `app.connection`, the `friend_engagement.py` router), but no aggregated surface where a user can see what their connections have been doing. We have plenty of activity-shaped signal already — My Shows additions, episode/season/show watch marks, and (after NEU-60) show + episode ratings — but it's only inspectable on a per-show or per-episode basis. There's no single chronological view of "what have my friends been up to?"

## Goal

Ship a per-user activity feed showing accepted connections' meaningful actions in reverse-chronological order: My Shows additions, watch marks (single episode, season, whole show), and ratings. The feed must roll up burst activity so a connection marking a whole season episode-by-episode doesn't produce ten line items, and must omit "negative" actions (unwatches, removes from My Shows, rating clears) — those instead remove the corresponding prior positive event.

## Non-goals

- **Reactions or comments on feed items.** Out of scope for v1.
- **Push or email notifications.** No background delivery; the feed is a pull-only surface.
- **"What's trending among my friends" aggregated discovery views.**
- **Per-verb privacy controls** (e.g., "broadcast my ratings but not my watches"). One global toggle + a per-show hide is enough for v1.
- **Backfilling pre-feature history.** Events are forward-going only; existing watches and ratings from before the feature ships do not retroactively appear in the feed.
- **Show-blocking outside My Shows.** Per-show hiding lives on `user_show_watch`; if you've never added the show, there's nothing to broadcast about it.

## Architecture

### Event store: `app.activity_event`

One table in the `app` schema. Every "positive" action (add, watch, rate) writes a row. Every "negative" action (remove, unwatch, clear rating) **deletes** the corresponding prior row instead of writing a new event. This makes undo semantics fall out automatically: deleting a row from `user_episode_watch` is the same shape as deleting the corresponding `activity_event` row.

```sql
CREATE TABLE app.activity_event (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    actor_id      UUID NOT NULL REFERENCES app.user(id) ON DELETE CASCADE,
    verb          TEXT NOT NULL,
    target_type   TEXT NOT NULL,          -- 'show' | 'season' | 'episode'
    target_id     BIGINT NOT NULL,        -- show_id for show/season verbs; episode_id otherwise
    season_number INT,                    -- only set when verb='watched_season' (and target_id holds the show_id)
    payload       JSONB,                  -- e.g., {"stars": 4.5} for rated_*
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_activity_event UNIQUE (actor_id, verb, target_type, target_id, season_number)
);
CREATE INDEX ix_activity_event_actor_created ON app.activity_event(actor_id, created_at DESC);
CREATE INDEX ix_activity_event_target        ON app.activity_event(target_type, target_id);
```

The `UNIQUE` constraint enforces single-occurrence semantics per `(actor, verb, target)`:
- Re-watching the same episode → `INSERT … ON CONFLICT DO UPDATE SET created_at = now()`.
- Re-rating the same show → `ON CONFLICT DO UPDATE SET payload = EXCLUDED.payload, created_at = now()`.
- A user can't accidentally produce two `added_show` events for the same show; cancel-on-remove + re-add will reappear with a new timestamp.

`target_type='season'` uses `target_id = show_id` plus `season_number`. The composite uniqueness lives in `uq_activity_event` and is well-defined because `season_number` is NULL for non-season verbs (PostgreSQL treats NULLs in a `UNIQUE` constraint as distinct, so single-target verbs still uniquely key on `(actor, verb, type, id)` because all rows share `season_number IS NULL`).

### Verbs and the mutation matrix

| Mutation | Emits | Undo | Undo action |
|---|---|---|---|
| `PUT /me/shows/{id}` (add) | `added_show(target_type=show, target_id=id)` | `DELETE /me/shows/{id}` | Delete the `added_show` row |
| `POST /me/episodes/{id}/watched` | `watched_episode(target_type=episode, target_id=id)` | `DELETE` same | Delete the `watched_episode` row |
| `POST /me/shows/{id}/season/{n}/watched` | One `watched_season(target_type=show, target_id=id, season_number=n)` **+** delete any `watched_episode` rows for episodes belonging to that season | `DELETE` same | Delete the `watched_season` row |
| `POST /me/shows/{id}/watched` | One `watched_show(target_type=show, target_id=id)` **+** delete any `watched_season` / `watched_episode` rows for that show | `DELETE` same | Delete the `watched_show` row |
| `PUT /me/shows/{id}/rating` | `rated_show(target_type=show, target_id=id, payload={"stars":…})` | `DELETE` same | Delete the `rated_show` row |
| `PUT /me/episodes/{id}/rating` | `rated_episode(target_type=episode, target_id=id, payload={"stars":…})` | `DELETE` same | Delete the `rated_episode` row |

**Write-time collapse** for the two bulk endpoints is the key user-requested behavior: marking a whole season produces one season-level event and tombstones any per-episode events for that season already in the feed. Marking a whole show does the same up one level. This ensures the feed never shows "Alice watched S2E1 · Alice watched S2E2 · … · Alice marked Season 2 watched" — only the aggregate.

### The emit/cancel helper

A single service module (`app/services/activity_service.py`) exposes:

```python
async def emit(db, *, actor_id, verb, target_type, target_id, season_number=None, payload=None): ...
async def cancel(db, *, actor_id, verb, target_type, target_id, season_number=None): ...
async def collapse_for_season(db, *, actor_id, show_id, season_number): ...  # called by bulk-season emit
async def collapse_for_show(db, *, actor_id, show_id): ...                   # called by bulk-show emit
```

`emit` is an upsert. `cancel` is an idempotent delete. The routes call these synchronously in the same transaction as the underlying watch/rating mutation — there's no background worker, no event bus, no eventual consistency.

### Read-time aggregation: dribble rollup

Write-time collapse covers the bulk endpoints. It doesn't cover users who mark 5 individual episodes of one show over 20 minutes; we can't know at write time whether a 6th is coming. The feed query post-aggregates:

> Consecutive `watched_episode` events from the same actor, against episodes of the same show, where each is ≤ `ACTIVITY_ROLLUP_WINDOW_MIN` (default **30 minutes**) from the previous one, fold into a single `watched_episode_run` rollup item: "Alice watched 5 episodes of *Severance*."

The boundary breaks on: different show, different actor, or a gap > 30 min. Single-event runs render normally ("Alice watched *Severance* S2E5"). The rollup item is identified by `min(id)` of the constituent events for stable cursor pagination.

A SQL window function (`LAG(created_at) OVER (PARTITION BY actor_id, show_id ORDER BY created_at)`) partitioned per actor/show gives the gap; a running sum produces group IDs; aggregation collapses each group. Implementation lives in `app/repos/activity_repo.py`.

Window size is a constant in `config.py`; not user-configurable in v1.

### Privacy

Two layers:

1. **Global broadcast toggle.** New column `app.user.activity_feed_enabled BOOL NOT NULL DEFAULT TRUE`. When `FALSE`, the user's events are excluded from every friend's feed. Existing event rows are not deleted — soft hide, reversible.
2. **Per-show hide.** New column `app.user_show_watch.hide_from_activity BOOL NOT NULL DEFAULT FALSE`. When `TRUE`, events whose target is that show (`target_type='show' AND target_id=show_id` OR `target_type='episode' AND episode.show_id=show_id`) are excluded from broadcast.

The feed query joins `app.user` (filter `activity_feed_enabled = TRUE` on the actor) and `app.user_show_watch` (LEFT JOIN by actor + resolved show; filter `hide_from_activity IS NOT TRUE`).

Episode events resolve to a show via `tvmaze.episode.show_id`. Season events already carry `target_id = show_id`.

### Feed endpoint

```
GET /me/feed?cursor=<opaque>&limit=20
```

Auth required, no CSRF (pure read). Returns up to `limit` (capped at 50) items in reverse chronological order from accepted connections, applying privacy filters.

Cursor is an opaque base64 of `(created_at, id)` from the last item of the previous page — keyset pagination, no offset. The first page is requested without `cursor`. Response includes `next_cursor: string | null` for the next page.

Item shape:

```json
{
  "id": "stable-rollup-id",
  "actor": {"user_id": "uuid", "display_name": "Alice"},
  "kind": "added_show | watched_episode | watched_episode_run | watched_season | watched_show | rated_show | rated_episode",
  "show": {"id": 123, "name": "Severance"},
  "episode": {"id": 9, "name": "...", "season": 2, "number": 5},
  "season_number": null,
  "rollup_count": null,
  "stars": null,
  "occurred_at": "2026-05-13T17:42:00Z"
}
```

- `episode` is populated for `watched_episode` and `rated_episode`.
- `season_number` is populated for `watched_season`.
- `rollup_count` is populated for `watched_episode_run`.
- `stars` is populated for `rated_show` / `rated_episode`.

### Frontend surface

- New home-page tab **"Friends"** alongside My Shows / Watch Next / Upcoming. Cursor-paginated infinite scroll; each item links to its show/episode, the actor's display name links to `/u/{user_id}`.
- Settings surface gains a single toggle: "Share my activity with friends." Maps to `activity_feed_enabled`.
- `ShowDetailPage` gains a toggle "Hide this show from my activity feed," next to the existing My Shows controls. Disabled (greyed) when the show is not in My Shows — the column lives on `user_show_watch` so it requires a row.

### Caching matrix

- `GET /me/feed` — `Cache-Control: no-store`. Per-user, per-page, churns constantly.
- `PATCH /me/preferences` and `PATCH /me/shows/{id}/hide-from-activity` — no-store, CSRF required.

## Testing strategy

**Backend unit tests:**
- `emit` / `cancel` idempotency.
- The rollup-window SQL: a small fixture of `watched_episode` rows constructs every boundary case (same actor + same show within window → fold; gap > window → split; different show → split; different actor → split).
- The cancel-on-undo helper: emit then cancel returns the table to its original state.

**Backend integration tests** (ASGITransport, seeded DB):
- Each mutation route writes the correct event row (single row per `(actor, verb, target)` due to UNIQUE).
- Each undo route deletes the corresponding event row and does not emit a new event.
- Bulk-season mark collapses prior per-episode events for that season into one season event.
- Bulk-show mark collapses prior season + episode events for that show into one show event.
- Re-rating the same target updates the existing event row's `payload` and `created_at`; doesn't create a second row.
- `GET /me/feed` returns events from accepted connections only (pending and blocked excluded).
- Events from `activity_feed_enabled = FALSE` users are excluded.
- Events targeting a show with `hide_from_activity = TRUE` (the actor's flag) are excluded.
- Cursor pagination is stable across insertions: paging after the cursor returns rows strictly older than the cursor's `(created_at, id)`.
- Read-time rollup folds consecutive single-episode events within window; breaks across windows; breaks across shows; breaks across actors.

**Frontend tests:**
- `FriendsTab` renders each `kind` (mocked via MSW).
- Infinite scroll fetches next page via cursor.
- Per-show "hide" toggle disables when the show isn't in My Shows.
- Global broadcast toggle persists and reflects on revisit.

## Migration / rollout

1. Alembic migration creates `app.activity_event`, adds `app.user.activity_feed_enabled`, adds `app.user_show_watch.hide_from_activity`.
2. Deploy backend; mutations begin emitting events. Existing data does not retroactively become events.
3. Deploy frontend; Friends tab appears empty for the first hour or so (depending on user activity) and fills as friends do things.
4. No backfill, no rollback complexity beyond `DROP TABLE / DROP COLUMN`.

## Open questions resolved during brainstorm

- **Event shape:** single polymorphic table (`activity_event`), not per-verb tables.
- **Which mutations emit:** add show, watch (episode/season/show), rate (show/episode). Unwatches/removes/clears delete prior events instead.
- **Rollup:** write-time collapse on bulk routes + read-time rollup of consecutive same-show single-episode events within a 30-minute window.
- **Privacy:** global on by default; one user-level disable toggle; per-show hide on `user_show_watch`. No per-verb opt-out.
- **Feed surface:** new "Friends" home tab. No dedicated route required.
- **Backfill:** none. Forward-going only.
