# Episode alerts are schedule-derived, and catalog events are recorded only for tracked shows

**Status:** accepted (2026-09-26)
**Context:** [ADR-0002](./0002-no-upstream-api-in-request-path.md), [ADR-0005](./0005-shows-deleted-upstream-are-tombstoned.md), the *tvbf: Push Notifications* project spec (`docs/specs/tvbf-push-notifications-project-spec.md`)

Push notifications need to know when something *changed*, and the daily delta has never
computed that: it re-fetches a changed show in full and upserts it without reading the row it
overwrites. This project adds a change-event layer, and two decisions about its shape are
recorded here because the obvious alternative to each is the one a reader will reach for.

## 1. "A new episode" is a schedule fact, not a catalog event

The alert users want is *an episode of a show I track airs today*. The change the delta can
see is *an episode row appeared*, which is a different thing: TMDB lists episodes weeks or
months ahead, and lists a streaming drop's whole season in one payload. An event per new row
would fire early, fire ten times for one release, and fire again for every row a catch-up run
happens to touch.

So there is **no `episode_added` event**. The airs-today set is derived each morning from the
mirror as it stands — corrected `air_date = today`, season > 0, tracked, unwatched — the same
way `/me/upcoming` already reads it. That makes the episode alert immune to backfill bursts by
construction rather than by a guard, and it means a delivery run is repeatable from the
schedule alone. Premiere-date and status changes have no such standing fact to read, which is
why *those* are events.

## 2. Events are recorded for tracked shows only

The delta re-fetches a few thousand shows a night out of ~229k; a few hundred are tracked.
Diffing every re-fetched show would keep a history for shows nobody follows yet, at a cost —
one extra read per show plus a table that grows with the catalog — paid for a case that is
answered acceptably another way: a show added to My Shows starts producing events that night.
Nothing here needs the past.

**What would reverse this.** A feature that reads catalog history for its own sake — a
"what changed this week" surface over the whole catalog — would want every show diffed, and
would want the diff to move from Python into the database. Until then the extra read is a
`WHERE show_id IN (tracked)` and the table stays small.

## Consequences

- The airs-today notification has no event row and no `observed_at`; its notification key
  is the episode and the air date, and the delivery log is the only record it was sent.
- The full catalog pass records no events at all — it has nothing to compare against.
- A premiere date that arrives for an untracked show and is tracked the next day produces no
  "premiere set" alert; the airs-today alert on the day still fires.
