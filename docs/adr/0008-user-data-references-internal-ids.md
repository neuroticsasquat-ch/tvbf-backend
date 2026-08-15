# User data references internal ids, never an upstream vendor's

**Status:** accepted (2026-08-08)

`catalog.show.id` and `catalog.episode.id` are generated identities. The upstream key
(`tmdb_id`) sits alongside as a unique natural key that upserts conflict-target on.
Everything in `app` — watches, ratings, activity events — references the internal id.

## Why

We are adopting this while paying the bill for not having had it. `app.user_episode_watch.episode_id`
holds a **TV Maze primary key**. That one fact is why replacing the catalog source
(ADR-0007) requires an archive table, a reconciliation harness, a human-matching queue and
an exact-count acceptance test. Had the column held an internal id, swapping upstreams
would have been a catalog-only operation.

**It is what makes "no user data loss" expressible rather than merely intended.** The
constraint on the migration is absolute: no user loses a tracked show or a watched
episode, even where the upstream catalog has no counterpart. Consider a show TMDB does not
list. With user data pointed at `tmdb_id`, that watch record **cannot be represented** —
the only options are inventing a fake upstream id or dropping the row. With a surrogate,
it is ordinary: a locally-authored row with `tmdb_id IS NULL`, holding the user's history,
upgradeable in place if TMDB adds the show later.

That also demotes the community-edit escape hatch from load-bearing to optional. We can
hold the record locally and push it upstream at leisure, rather than blocking a user's
watch history on a third party's moderation queue.

**The cost is one column and one unique index**, and it is only this cheap while these
tables are being rewritten anyway.

## Consequences

Locally-authored catalog rows are a sanctioned, permanent mechanism — not a migration
artifact. `tmdb_id IS NULL` means "we hold this and upstream does not," and ingest must
never delete or overwrite such a row.

Upserts conflict-target `tmdb_id` rather than the primary key, so ingest code reads
slightly differently from the TV Maze original, where the two were the same column.

Reads join on internal ids. Any API surface exposing an id exposes the internal one; the
TMDB id is an ingest detail and should not leak into URLs or the SPA, or the trap is
re-armed one layer out.
