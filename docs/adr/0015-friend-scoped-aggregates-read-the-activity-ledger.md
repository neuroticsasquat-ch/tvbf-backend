# Friend-scoped aggregates read the activity ledger, not the watch tables

**Status:** accepted (2026-09-27)
**Context:** [ADR-0013](./0013-recommendations-are-generated-from-watch-behaviour.md), the *Friend Activity Feed* design (`docs/superpowers/specs/2026-05-13-friend-activity-feed-design.md`), the *tvbf: Popular with Friends* project spec (`docs/specs/tvbf-popular-with-friends-project-spec.md`)

Popular with Friends ranks shows by how many of the viewer's connections were recently active
on them. The obvious source for "recently watched" is `app.user_episode_watch.watched_at`, and
a reader will reach for it. It reads `app.activity_event` instead, and so must every later
friend-scoped aggregate, for three reasons that are properties of the data rather than of this
feature.

## 1. The watch tables do not record *when the user acted*

`watched_at` is stamped `now()` on a single mark, but a season or whole-show mark stamps every
episode with the same instant, the Next Episode import backdated rows to the episode's airdate,
and production values span 1994 to 2026. A catch-up of sixty episodes reads as sixty watches
this minute; a rewatch keeps the first timestamp (`ON CONFLICT DO NOTHING`). The activity
ledger is the one place a user's *action* is recorded at the grain it was taken: one row per
(actor, verb, target), a bulk mark collapsed to one row, and cancel-on-undo so a mis-mark
leaves nothing behind. "Active on a show in the last fourteen days" is a question only the
ledger can answer.

## 2. The sharing switches are defined on the ledger

`app.user.activity_feed_enabled` and `app.user_show_watch.hide_from_activity` were introduced
as feed controls, and the published privacy copy promises that a hidden show or a switched-off
user does not appear in what friends see of their activity. An aggregate over the watch tables
would have to re-implement those predicates or silently bypass them. Reading the ledger
inherits them: the same two joins the feed query makes, and nothing else.

## 3. Aggregating the ledger is cheap; the watch tables are not indexed for it

`ix_activity_event_actor_created (actor_id, created_at)` is exactly the shape a
friends-in-window scan wants. `user_episode_watch` has only its primary key, which leads on
`user_id` and would need a `catalog.episode` join per row to reach a show.

**What this rules out.** A friend-scoped surface that needs a fact the ledger does not carry —
"how many episodes did they watch", a per-episode progress bar — is a different question and
may read the watch tables for it, but it must still gate on the switches. The switches are not
optional on any surface that aggregates activity across a connection.

**What would reverse this.** Backfilling the ledger from the watch tables (rejected by the feed
design: forward-going only) would let a fresh account's history count; nothing here needs it.
