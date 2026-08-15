"""Move user history onto the ingested episode rows, then delete the copies (NEU-1126).

NEU-1042 copied every `tvmaze.episode` into `catalog.episode` with `tmdb_id IS
NULL`. NEU-1045 built the pass that would have stamped a `tmdb_id` onto each one
— and it merged on 2026-08-11, a day *after* the full catalog ingest started, so
the window it depends on had already shut. Nothing recorded that a merged pass
still owed a production run, which is the failure this repo now keeps a run log
for.

The result, measured against production on 2026-08-11: 2,690,633 copied episodes
sit under a matched show beside an ingested twin, and **all 7,137 watched-or-rated
episodes point at the copy.** Not one user watch resolved to TMDB-sourced data.

## Why this re-points instead of mapping

`upsert_episodes` conflict-targets `tmdb_id` (ADR-0008), so once the ingest has
run every episode TMDB knows about already holds its id and `uq_episode_tmdb_id`
refuses the cheap mapping. Running `task map:episodes` today would map **zero**
rows while spending ~62,000 TMDB requests — 1,909,367 collide and 760,254 have no
counterpart. This is the same wall NEU-1119 hit at season grain and NEU-1066 at
show grain, and the same answer: retire the copy in favour of the ingested row.

## What makes this pass different from its siblings

`season_dedupe` goes out of its way *not* to touch `app`, and so did
`show_prune` before NEU-1051 deleted it. This one has to: the user rows are the
whole point. Three consequences, and each
is a place the pass could quietly cost somebody their history.

**Three write sites, not two.** `app.user_episode_watch` and
`app.user_episode_rating` carry foreign keys, so forgetting one fails loudly.
`app.activity_event` is polymorphic with **no foreign key at all** — it neither
blocks nor cascades, it silently orphans — and it is the site NEU-1066's
five-site rule exists for.

**Every write site has a uniqueness constraint the re-point can collide with.**
`user_episode_watch` is keyed `(user_id, episode_id)`, `user_episode_rating`
carries `uq_user_episode_rating` on the same pair, and `uq_activity_event` is
`(actor_id, verb, target_type, target_id, season_number)` NULLS NOT DISTINCT. A
user holding rows on *both* the copy and its twin cannot have the copy's row
moved onto the twin — the merge would collapse two records into one and the
reconciliation harness would read it as a loss. So each write carries a
`NOT EXISTS` guard, and **a copied row whose user data could not move is kept**,
counted as `blocked_by_collision`, rather than deleted out from under it.
Production has zero of these today; the constraints make the state representable,
which is the only reason it needs handling.

**The delete re-asserts that nothing references the row.** It is the statement
that destroys data, so "no user row points here any more" is a predicate on the
`DELETE` itself rather than a property of whichever query built the work list —
the same stance `season_dedupe`'s `_DELETE` takes with `sh.tmdb_id IS NOT NULL`.

## Pairing, and the ambiguity the ticket predicted backwards

A copy pairs with its twin on `(show_id, season_number, episode_number)`, the key
NEU-1045 used, and **only when exactly one row stands on each side of it.**

The ticket anticipated ambiguity from multiple *ingested* twins. Measured against
production, there are **none** — but there are 443 keys where two or more
*copied* rows share a single twin (889 rows), which is TV Maze's own duplicate
numbering arriving through the copy. That direction is worse, not better:
re-pointing both copies onto one twin merges two watch records into one, and
`(user_id, episode_id)` would either reject it or silently lose a row. Both
directions are therefore refused, and neither is resolved by primary key.

No user-touched episode falls in an ambiguous bucket today: of the 7,137,
**6,948** have exactly one twin and one copy, and **189** have no twin at all.

**A copied special can never pair, and needs no special case.** NEU-1042
numbered TV Maze's null-numbered specials *negative* within their season; no
ingested row carries a negative `episode_number`, so those rows simply find no
twin and are kept — which is what should happen to a row with no TMDB
counterpart anyway.

## The work list is walked by primary key, not re-derived per batch

`season_dedupe` re-runs its whole work-list query for every batch, which is
affordable over 188,134 seasons. Here the candidate set is 2.69M rows against
6.5M ingested ones, and grouping both on every batch would dominate the run. So
the loop keeps a keyset cursor (`c.id > :after`) and probes each candidate's key
through `ix_episode_show_id_season_number` instead — every row is visited once
across the whole pass.

That costs the property `season_dedupe` gets for free, and it is worth being
explicit: a batch that fails leaves the cursor behind it, so **the resumption
point is a re-run from the start**, not the cursor. That is cheap, because a
re-run only sees what is genuinely still there — the re-pointed rows are gone.

## Reverting took two statements, and there is no first one any more

`task copy:catalog` put the deleted episode rows back under their original ids —
its anti-join verification demanded a catalog row per `tvmaze.episode`. It did
**not** put the user rows back, because it never touched `app`. NEU-1051 deleted
that pass with the `tvmaze` schema, so the first statement is gone and only the
pre-drop dump can supply it. The second, recorded because it is what a restore
would still need, re-derives the pairing in reverse:

    UPDATE app.user_episode_watch w
       SET episode_id = c.id
      FROM catalog.episode t
      JOIN LATERAL (
             SELECT max(e.id) AS id, count(*) AS n
               FROM catalog.episode e
              WHERE e.show_id = t.show_id
                AND e.season_number = t.season_number
                AND e.episode_number = t.episode_number
                AND e.tmdb_id IS NULL
           ) c ON c.n = 1
     WHERE t.id = w.episode_id
       AND t.tmdb_id IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM app.user_episode_watch x
                        WHERE x.user_id = w.user_id AND x.episode_id = c.id)

and the same shape for `user_episode_rating` and for `activity_event` where
`target_type = 'episode'`.

**The revert carries the forward pass's two guards, and needs them for the same
reasons.** `c.n = 1` refuses a key whose copies are ambiguous, which is the 443
keys `_CANDIDATES` already declines — without it `UPDATE ... FROM` picks one
non-deterministically, which is exactly what the acceptance criteria forbid. The
`NOT EXISTS` refuses a move that would collide, which is what stops the revert
aborting on a `blocked_by_collision` row it was never responsible for.

**And it is deliberately wider than this pass.** It matches every user row on an
ingested episode that has a copy beneath it, not only the rows this pass moved —
nothing records which those were. That is the right behaviour for a full revert
of the migration and the wrong tool for undoing one batch; there is no undo at
that granularity, which is what `--limit` is for.
"""

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.tmdb import user_history

log = logging.getLogger(__name__)

# The floor and its refusal were `show_prune`'s until NEU-1051 deleted that
# pass along with the `tvmaze` schema its guard read. They live here now
# because this is the only pass left that asks the question. The value is the
# tombstone's `_MIN_FEED_ABSOLUTE`, set the same way: comfortably below a real
# catalog (228,841 series ingested in production), far above anything a partial
# pass would leave behind.
MIN_INGESTED_SHOWS = 150_000


class IngestNotRun(Exception):
    """Too few ingested shows for the work list to mean what it claims.

    Raised rather than returning an empty result: a pass that quietly did
    nothing and a pass that quietly re-pointed the wrong 1.9M rows are the two
    failures this guard sits between, and only one of them is loud on its own.
    """


# Episodes per transaction. Sized so 1.9M rows is a few hundred round trips while
# killing the pass costs one batch. The ids travel as two arrays rather than a
# bind per row, so Postgres's 32,767-parameter cap — the ceiling behind
# `_EPISODE_BATCH_SIZE` — never enters into it.
BATCH_SIZE = 5_000

# One candidate copy and the twin it defers to.
#
# The lateral probe replaces the two whole-table aggregates the obvious query
# needs, and `t.twins = 1 AND t.copies = 1` is where both ambiguity directions are
# refused: more than one ingested row for the key has no answer to "which twin",
# and more than one copied row would merge two user records into one. `min(id)
# FILTER` is therefore reading the only row in its group, not picking a winner.
_CANDIDATES = text("""
    SELECT c.id AS doomed_id, t.twin_id AS survivor_id
      FROM catalog.episode c
      JOIN catalog.show sh ON sh.id = c.show_id
     CROSS JOIN LATERAL (
             SELECT min(e.id) FILTER (WHERE e.tmdb_id IS NOT NULL) AS twin_id,
                    count(*) FILTER (WHERE e.tmdb_id IS NOT NULL) AS twins,
                    count(*) FILTER (WHERE e.tmdb_id IS NULL) AS copies
               FROM catalog.episode e
              WHERE e.show_id = c.show_id
                AND e.season_number = c.season_number
                AND e.episode_number = c.episode_number
           ) t
     WHERE c.tmdb_id IS NULL
       AND sh.tmdb_id IS NOT NULL
       AND c.id > :after
       AND t.twins = 1
       AND t.copies = 1
     ORDER BY c.id
     LIMIT :limit
""")

# Which `(episode, user)` pairs cannot move, decided **once, before any write.**
#
# Each `NOT EXISTS` in the three UPDATEs below is enough to stop that statement
# raising, but on its own it decides per *row* — so a user whose activity event
# collides while their watch does not would have the watch moved to the twin and
# the event left behind on the copy, splitting one person's history across two
# episode rows. That is worse than either whole answer.
#
# The unit of refusal is therefore `(episode, user)`: all of one person's rows on
# a copy move together or none of them do. Deliberately not the whole episode —
# that would let one user's collision strand every *other* user's history on a
# row nobody else has a conflict with.
#
# The three arms mirror `user_episode_watch`'s primary key, `uq_user_episode_rating`
# and `uq_activity_event` exactly, including `IS NOT DISTINCT FROM` for the latter's
# NULLS NOT DISTINCT season number.
_COLLIDING = text("""
    SELECT DISTINCT m.doomed_id, u.user_id
      FROM unnest(cast(:doomed AS bigint[]), cast(:survivors AS bigint[]))
             AS m(doomed_id, survivor_id)
     CROSS JOIN LATERAL (
             SELECT w.user_id
               FROM app.user_episode_watch w
              WHERE w.episode_id = m.doomed_id
                AND EXISTS (SELECT 1 FROM app.user_episode_watch x
                             WHERE x.user_id = w.user_id
                               AND x.episode_id = m.survivor_id)
              UNION
             SELECT r.user_id
               FROM app.user_episode_rating r
              WHERE r.episode_id = m.doomed_id
                AND EXISTS (SELECT 1 FROM app.user_episode_rating x
                             WHERE x.user_id = r.user_id
                               AND x.episode_id = m.survivor_id)
              UNION
             SELECT a.actor_id
               FROM app.activity_event a
              WHERE a.target_type = 'episode'
                AND a.target_id = m.doomed_id
                AND EXISTS (SELECT 1 FROM app.activity_event x
                             WHERE x.actor_id = a.actor_id
                               AND x.verb = a.verb
                               AND x.target_type = 'episode'
                               AND x.target_id = m.survivor_id
                               AND x.season_number IS NOT DISTINCT FROM a.season_number)
           ) u(user_id)
""")

# The pair list `_COLLIDING` produced, as a predicate the three writes share.
_NOT_BLOCKED = """
    NOT EXISTS (
        SELECT 1 FROM unnest(cast(:blocked_episodes AS bigint[]),
                             cast(:blocked_users AS uuid[])) AS b(episode_id, user_id)
         WHERE b.episode_id = m.doomed_id AND b.user_id = {owner}
    )
"""

# The three write sites, built from the shared machinery in `user_history` —
# NEU-1146 extracted them there so its own pass could not drift from this one on
# which uniqueness constraint each table carries. `_NOT_BLOCKED` withholds a
# whole person's rows when any one of them would collide; the per-table
# `NOT EXISTS` inside each statement stays as the backstop that keeps a
# constraint violation from taking the batch down if the two ever disagree.
#
# The withholding is this pass's policy and not the shared module's: NEU-1146
# reverses it, deleting the redundant row where this one keeps it (§4.2). That
# is exactly why `extra` is injected rather than baked in.
_WRITES = user_history.episode_statements(lambda owner: _NOT_BLOCKED.format(owner=owner))
_REPOINT_WATCH = _WRITES.watch
_REPOINT_RATING = _WRITES.rating
_REPOINT_ACTIVITY = _WRITES.activity

# `_STILL_REFERENCED` is both the delete's guard and the pass's honesty: a copy
# whose user rows could not move keeps its row, so nothing is deleted out from
# under a watch record. The predicates from `_CANDIDATES` are re-asserted for the
# reason `season_dedupe._DELETE` re-asserts its own — this is the statement that
# destroys data no feed can restore.
_STILL_REFERENCED = user_history.EPISODE_STILL_REFERENCED

_DELETE = text(f"""
    DELETE FROM catalog.episode e
     USING catalog.show sh
     WHERE e.id = ANY(cast(:doomed AS bigint[]))
       AND sh.id = e.show_id
       AND e.tmdb_id IS NULL
       AND sh.tmdb_id IS NOT NULL
       AND NOT ({_STILL_REFERENCED})
""")

_BLOCKED = text(f"""
    SELECT count(*) FROM catalog.episode e
     WHERE e.id = ANY(cast(:doomed AS bigint[]))
       AND ({_STILL_REFERENCED})
""")

# The whole grain, bucketed. Set-based rather than the lateral probe the loop
# uses: this runs once, where the probe runs per batch.
_BUCKETS = """
    WITH copied AS (
             SELECT e.id, e.show_id, e.season_number, e.episode_number,
                    sh.tmdb_id AS show_tmdb_id
               FROM catalog.episode e
               JOIN catalog.show sh ON sh.id = e.show_id
              WHERE e.tmdb_id IS NULL
         ),
         copied_keys AS (
             SELECT show_id, season_number, episode_number, count(*) AS copies
               FROM copied
              WHERE show_tmdb_id IS NOT NULL
              GROUP BY show_id, season_number, episode_number
         ),
         ingested_keys AS (
             SELECT show_id, season_number, episode_number, count(*) AS twins
               FROM catalog.episode
              WHERE tmdb_id IS NOT NULL
              GROUP BY show_id, season_number, episode_number
         ),
         classified AS (
             SELECT c.id,
                    coalesce(k.copies, 1) AS copies,
                    coalesce(i.twins, 0) AS twins
               FROM copied c
               LEFT JOIN copied_keys k
                      ON k.show_id = c.show_id
                     AND k.season_number = c.season_number
                     AND k.episode_number = c.episode_number
               LEFT JOIN ingested_keys i
                      ON i.show_id = c.show_id
                     AND i.season_number = c.season_number
                     AND i.episode_number = c.episode_number
              WHERE c.show_tmdb_id IS NOT NULL
         )
"""

_COUNTS = text(f"""
    {_BUCKETS},
         touched AS (
             SELECT cl.id, cl.copies, cl.twins
               FROM classified cl
               JOIN catalog.episode e ON e.id = cl.id
              WHERE {_STILL_REFERENCED}
         )
    SELECT (SELECT count(*) FROM classified WHERE twins = 1 AND copies = 1) AS repointable,
           (SELECT count(*) FROM classified WHERE twins = 0) AS kept_no_counterpart,
           (SELECT count(*) FROM classified WHERE twins = 1 AND copies > 1)
               AS kept_ambiguous_copies,
           (SELECT count(*) FROM classified WHERE twins > 1) AS kept_ambiguous_twins,
           (SELECT count(*) FROM catalog.episode e
              JOIN catalog.show sh ON sh.id = e.show_id
             WHERE e.tmdb_id IS NULL AND sh.tmdb_id IS NULL) AS kept_under_unmatched_show,
           (SELECT count(*) FROM touched WHERE twins = 1 AND copies = 1)
               AS user_touched_repointable,
           (SELECT count(*) FROM touched WHERE NOT (twins = 1 AND copies = 1))
               AS user_touched_kept,
           (SELECT count(*) FROM app.user_episode_watch w
              JOIN classified cl ON cl.id = w.episode_id
             WHERE cl.twins = 1 AND cl.copies = 1) AS watches_to_move,
           (SELECT count(*) FROM app.user_episode_rating r
              JOIN classified cl ON cl.id = r.episode_id
             WHERE cl.twins = 1 AND cl.copies = 1) AS ratings_to_move,
           (SELECT count(*) FROM app.activity_event a
              JOIN classified cl ON cl.id = a.target_id
             WHERE a.target_type = 'episode'
               AND cl.twins = 1 AND cl.copies = 1) AS activity_to_move
""")

# Every `(show, season number, episode number)` that would still carry more than
# one row once the pass has run — the residue of the "no show carries two rows"
# criterion, enumerated the way `season_dedupe`'s `still_doubled` is rather than
# left for someone to rediscover. Three shapes reach it and only the third is the
# one the ticket named:
#
# * `twins` 0, `copies` above 1 — TV Maze's own duplicate numbering on a key TMDB
#   has no episode for. Neither row has a counterpart to defer to.
# * `twins` 1, `copies` above 1 — the same duplicate numbering where TMDB *does*
#   have the episode. Re-pointing both copies onto one twin would merge two user
#   records, so both are kept.
# * `twins` above 1 — two rows the ingest itself wrote for one key, which is the
#   ambiguity `_CANDIDATES` refuses. None in production.
_STILL_DOUBLED = text(f"""
    {_BUCKETS}
    SELECT e.show_id,
           e.season_number,
           e.episode_number,
           count(*) AS row_count,
           count(*) FILTER (WHERE e.tmdb_id IS NOT NULL) AS ingested_rows,
           bool_or({_STILL_REFERENCED}) AS carries_user_data
      FROM catalog.episode e
      JOIN catalog.show sh ON sh.id = e.show_id
     WHERE sh.tmdb_id IS NOT NULL
       AND NOT EXISTS (
             SELECT 1 FROM classified cl
              WHERE cl.id = e.id AND cl.twins = 1 AND cl.copies = 1
           )
     GROUP BY e.show_id, e.season_number, e.episode_number
    HAVING count(*) > 1
     ORDER BY count(*) DESC, e.show_id, e.season_number, e.episode_number
""")

_INGESTED_SHOWS = text("SELECT count(*) FROM catalog.show WHERE tmdb_synced_at IS NOT NULL")


class EpisodeRepointAborted(Exception):
    """A batch did not account for every row it selected. The message is what to read."""


@dataclass(frozen=True)
class RepointResult:
    """What one run of the pass actually did."""

    episodes_deleted: int
    watches_repointed: int
    ratings_repointed: int
    activity_repointed: int
    blocked_by_collision: int
    batches: int


@dataclass(frozen=True)
class RepointReport:
    """The state of the episode grain, as counts a person can act on.

    Flat and JSON-shaped for the same reason `season_dedupe`'s report is: it is
    read in a terminal and piped over `ssh docker exec`.
    """

    repointable: int
    watches_to_move: int
    ratings_to_move: int
    activity_to_move: int
    user_touched_repointable: int
    user_touched_kept: int
    kept_no_counterpart: int
    kept_ambiguous_copies: int
    kept_ambiguous_twins: int
    kept_under_unmatched_show: int
    still_doubled: tuple[dict[str, int | bool], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "repointable": self.repointable,
            "watches_to_move": self.watches_to_move,
            "ratings_to_move": self.ratings_to_move,
            "activity_to_move": self.activity_to_move,
            "user_touched_repointable": self.user_touched_repointable,
            "user_touched_kept": self.user_touched_kept,
            "kept_no_counterpart": self.kept_no_counterpart,
            "kept_ambiguous_copies": self.kept_ambiguous_copies,
            "kept_ambiguous_twins": self.kept_ambiguous_twins,
            "kept_under_unmatched_show": self.kept_under_unmatched_show,
            "still_doubled": [dict(row) for row in self.still_doubled],
        }


async def ingested_show_count(db: AsyncSession) -> int:
    """How many shows carry the full ingest's watermark.

    Public because the report has no floor of its own but still has to say when
    its counts are reading low for the reason the floor exists.
    """
    return (await db.execute(_INGESTED_SHOWS)).scalar_one()


async def _assert_ingest_ran(db: AsyncSession, floor: int) -> None:
    ingested = await ingested_show_count(db)
    if ingested < floor:
        raise IngestNotRun(
            f"{ingested} show(s) carry a tmdb_synced_at, under the floor of "
            f"{floor} — run the full TMDB catalog ingest first, or almost no "
            f"copied episode has a twin and this pass reads as a no-op"
        )


async def repoint_episodes(
    db: AsyncSession,
    *,
    limit: int | None = None,
    batch_size: int = BATCH_SIZE,
    min_ingested: int = MIN_INGESTED_SHOWS,
) -> RepointResult:
    """Move user rows onto the ingested twin, then delete the copy, a batch per transaction.

    `limit` caps how many copied episodes the run retires, which is how to try a
    hundred before spending the full pass. Idempotent and resumable: a row leaves
    the work list by being deleted, so a re-run costs only what is genuinely still
    there — and because the cursor is not persisted, a re-run starts from the
    beginning and finds exactly that.
    """
    await _assert_ingest_ran(db, min_ingested)

    episodes_deleted = 0
    watches = ratings = activity = blocked = batches = consumed = 0
    after = 0

    while True:
        # Against candidates consumed, not rows deleted: a smoke run that kept
        # hitting kept rows would otherwise scan far past the N it was given.
        size = batch_size if limit is None else min(batch_size, limit - consumed)
        if size <= 0:
            break

        rows = (await db.execute(_CANDIDATES, {"after": after, "limit": size})).all()
        if not rows:
            break

        doomed = [row.doomed_id for row in rows]
        survivors = [row.survivor_id for row in rows]

        # Whole people drop out, not individual rows — see `_COLLIDING`.
        blocked_pairs = (
            await db.execute(_COLLIDING, {"doomed": doomed, "survivors": survivors})
        ).all()
        params = {
            "doomed": doomed,
            "survivors": survivors,
            "blocked_episodes": [pair.doomed_id for pair in blocked_pairs],
            "blocked_users": [pair.user_id for pair in blocked_pairs],
        }

        # `RETURNING` on the first two, so their counts come from the rows
        # themselves; the activity statement has none and still reports rowcount.
        moved_watches = len((await db.execute(_REPOINT_WATCH, params)).all())
        moved_ratings = len((await db.execute(_REPOINT_RATING, params)).all())
        moved_activity = await db.execute(_REPOINT_ACTIVITY, params)
        deleted = await db.execute(_DELETE, {"doomed": doomed})
        still_referenced = (await db.execute(_BLOCKED, {"doomed": doomed})).scalar_one()

        # Every selected row is either gone or deliberately kept — kept meaning a
        # user row still points at it, which is the only thing `_DELETE`
        # withholds for. A third outcome means `_DELETE` and `_CANDIDATES`
        # disagree about what is safe to touch, and since the cursor steps past
        # the row either way the pass would carry on having silently skipped it.
        # Stop instead, batch rolled back.
        if deleted.rowcount + still_referenced != len(doomed):  # type: ignore[attr-defined]
            await db.rollback()
            raise EpisodeRepointAborted(
                f"selected {len(doomed)} episode(s) but deleted "
                f"{deleted.rowcount} and kept {still_referenced}; "  # type: ignore[attr-defined]
                f"refusing to continue"
            )

        await db.commit()

        after = doomed[-1]
        consumed += len(doomed)
        episodes_deleted += deleted.rowcount  # type: ignore[attr-defined]
        watches += moved_watches
        ratings += moved_ratings
        activity += moved_activity.rowcount  # type: ignore[attr-defined]
        blocked += still_referenced
        batches += 1
        log.info(
            "batch %d: moved %d watch(es), %d rating(s), %d event(s); deleted %d "
            "episode(s) (%d total), kept %d still referenced",
            batches,
            moved_watches,
            moved_ratings,
            moved_activity.rowcount,  # type: ignore[attr-defined]
            deleted.rowcount,  # type: ignore[attr-defined]
            episodes_deleted,
            still_referenced,
        )

    return RepointResult(
        episodes_deleted=episodes_deleted,
        watches_repointed=watches,
        ratings_repointed=ratings,
        activity_repointed=activity,
        blocked_by_collision=blocked,
        batches=batches,
    )


async def build_report(db: AsyncSession) -> RepointReport:
    """Count what the pass would do and what it is deliberately leaving alone.

    Needs no TMDB credential and writes nothing — safe to run against production
    before deciding to spend the pass, and the thing to re-read afterwards to
    confirm `repointable` reached zero and to read `still_doubled`, which is what
    the "no show carries two rows" criterion actually scores against.
    """
    counts = (await db.execute(_COUNTS)).one()
    residue = (await db.execute(_STILL_DOUBLED)).all()
    return RepointReport(
        repointable=counts.repointable,
        watches_to_move=counts.watches_to_move,
        ratings_to_move=counts.ratings_to_move,
        activity_to_move=counts.activity_to_move,
        user_touched_repointable=counts.user_touched_repointable,
        user_touched_kept=counts.user_touched_kept,
        kept_no_counterpart=counts.kept_no_counterpart,
        kept_ambiguous_copies=counts.kept_ambiguous_copies,
        kept_ambiguous_twins=counts.kept_ambiguous_twins,
        kept_under_unmatched_show=counts.kept_under_unmatched_show,
        still_doubled=tuple(
            {
                "show_id": row.show_id,
                "season_number": row.season_number,
                "episode_number": row.episode_number,
                "rows": row.row_count,
                "ingested_rows": row.ingested_rows,
                "carries_user_data": row.carries_user_data,
            }
            for row in residue
        ),
    )
