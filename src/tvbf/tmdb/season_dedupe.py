"""Retire the copied season rows the TMDB ingest has already superseded (NEU-1119).

NEU-1042 copied every `tvmaze.season` into `catalog.season` with `tmdb_id IS
NULL`. NEU-1043 then mapped the show grain and NEU-1045 the episode grain —
nothing ever mapped the season grain. `upsert_seasons` conflict-targets
`tmdb_id` (ADR-0008), so for every matched show the full ingest inserted a
*second* season row alongside each copied one: same show, same number, one
holding TMDB data under a fresh surrogate and one holding TV Maze data under the
preserved id.

## Why this deletes rather than maps

The ticket left the choice open because it depends on whether the ingest has run.
It has: production reported 228,841 shows with a `tmdb_synced_at` on 2026-08-11,
so every series already holds its own `tmdb_id` and `uq_season_tmdb_id` refuses
the cheap mapping NEU-1045 used at episode grain. Delete is what is left.

That is affordable here for a reason that does not hold one grain up: **nothing
in `app` references a season.** Two foreign keys point at `catalog.season` and
neither costs anything: `catalog.episode.season_id` is `ON DELETE SET NULL` — the
same asymmetry ADR-0005 draws between seasons (deletable) and shows (tombstoned)
— and `catalog.season_network.season_id` is `CASCADE`, which is moot because the
copy writes only `show`, `season`, `episode` and `show_aka`, so a copied season
has no network rows to take with it (0 in production).

## The three populations, and why only one of them goes

Measured against production on 2026-08-11, of 188,134 copied seasons:

* **122,350 duplicates** — under a matched show, and an ingested row already
  carries that season number. These are what the pass deletes.
* **47,445 under an unmatched show** (`catalog.show.tmdb_id IS NULL`). Not
  duplicates at all: they are the only season data that show has, and deleting
  one destroys data no feed can restore. Untouched, and the `DELETE` re-asserts
  that predicate itself rather than trusting the work list to have excluded them.
* **18,339 under a matched show with no counterpart** — 18,292 where the ingest
  did mirror the show and TMDB simply has no season of that number, 47 where the
  ingest never reached the show at all. Both are kept, for the same reason
  `prune_missing_seasons` carries its `tmdb_id IS NOT NULL` guard: an
  authoritative payload must not take a season no feed can restore.

## The episodes have to move first, and the ticket predicted otherwise

NEU-1119 assumed the copied seasons were orphans — that `upsert_episodes`, which
builds its `{season_number: id}` map from a live query, had already re-pointed
their episodes onto the higher-id ingested row. It re-points only the episodes it
*writes*, and a copied episode with no `tmdb_id` is not one: the ingest inserts a
fresh row beside it (NEU-1066's problem at episode grain) and leaves the original
attached to the original season. Production has **2,125,419 episodes still hanging
off the doomed seasons, 7,120 of them watched**, so a bare `DELETE` would trip
`SET NULL` across all of them.

Hence re-point, then delete, in one transaction per batch. Re-pointing is not a
guess: the survivor is the same show and the same `season_number`, so it is
faithful substitution rather than re-homing — see `_REPOINT` for the one row in
production where that distinction has teeth.

## Ambiguity is refused, never resolved by primary key

A show with two `tmdb_id`-bearing rows for one season number has no answer to
"which one is this episode's season", so its copied rows are left in place and
counted. Production has zero such shows and TMDB was measured not to duplicate a
season number (0 of 754, `scripts/probe_tmdb_status_vocabulary.py`), but
`catalog.season` deliberately carries no `UNIQUE (show_id, season_number)` — the
state is representable, so the pass declines it rather than letting
`UPDATE ... FROM` pick a row non-deterministically.

## Re-runnable on purpose, and it must stay that way

There is no watermark, for the reason `enrichment.py` needs none: a row leaves the
work list by being deleted. That matters beyond resumability — the catalog delta
(NEU-1035) adds seasons to matched shows nightly, and any that lands on a number a
copied row still holds is a new duplicate. This is a pass to re-run, not a
one-shot.

The corollary is an ordering constraint in the other direction: **`task
copy:catalog` puts the deleted rows back.** Its anti-join verification demands a
catalog row for every `tvmaze.season`, so a re-run re-inserts each one under its
original id — which is why the copy must not be run casually afterwards, and why
`verify_copy` reports `catalog.season` short until it is.

**Reverting takes two statements, not one**, and this is the part it would be
easy to get wrong. The copy restores the season rows but *not* the episodes'
parentage: `_COPY_EPISODES` skips rows already present (`NOT EXISTS ... ce.id =
e.id`, backstopped by `ON CONFLICT (id) DO NOTHING`), so it never rewrites a
`season_id` this pass moved, and a bare re-copy hands back the seasons with no
episodes attached. `tvmaze.episode.season_id` still holds every original pointer
while that schema stands (NEU-1051 has not run), so the second statement is:

    UPDATE catalog.episode e
       SET season_id = te.season_id
      FROM tvmaze.episode te
     WHERE te.id = e.id AND te.season_id IS NOT NULL

Which is what makes the work reversible in full rather than in part.
"""

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

# Seasons per transaction. Sized against the measured ~17 episodes per doomed
# season, so a batch re-points ~8,500 episode rows — large enough that 122k
# seasons is a few hundred round trips, small enough that killing the pass costs
# one batch. The ids travel as two arrays rather than a bind per row, so
# Postgres's 32,767-parameter cap (the same ceiling behind `_EPISODE_BATCH_SIZE`)
# never enters into it.
BATCH_SIZE = 500

# A copied season under a matched show whose number an ingested row already
# holds, paired with the survivor it defers to.
#
# `n = 1` is the ambiguity refusal: a number carried by two `tmdb_id`-bearing
# rows drops out of the join entirely rather than having a survivor chosen for
# it. `min(id)` is therefore reading the only row in its group, not picking a
# winner.
_DOOMED = """
    SELECT s.id AS doomed_id,
           v.survivor_id
      FROM catalog.season s
      JOIN catalog.show sh ON sh.id = s.show_id
      JOIN (
             SELECT show_id,
                    season_number,
                    min(id) AS survivor_id,
                    count(*) AS n
               FROM catalog.season
              WHERE tmdb_id IS NOT NULL
              GROUP BY show_id, season_number
           ) v
        ON v.show_id = s.show_id
       AND v.season_number = s.season_number
       AND v.n = 1
     WHERE s.tmdb_id IS NULL
       AND sh.tmdb_id IS NOT NULL
"""

_SELECT_BATCH = text(f"""
    WITH doomed AS ({_DOOMED})
    SELECT doomed_id, survivor_id
      FROM doomed
     ORDER BY doomed_id
     LIMIT :limit
""")

# Both arrays are positionally paired, so `unnest` of the two together yields the
# batch as rows — two bind parameters for any batch size.
#
# The pointer follows the **season**, not the episode's own `season_number`. The
# two can already disagree — `catalog.episode.season_number` is denormalised, and
# the copy carried across one production row where it differs from the season it
# hangs off. Substituting the season's replacement preserves that disagreement
# rather than creating one; re-homing the episode by its own number instead would
# be this pass deciding which of the two fields is right, on evidence it does not
# have. Repairing that is a different job.
_REPOINT = text("""
    UPDATE catalog.episode e
       SET season_id = m.survivor_id
      FROM unnest(cast(:doomed AS bigint[]), cast(:survivors AS bigint[]))
             AS m(doomed_id, survivor_id)
     WHERE e.season_id = m.doomed_id
""")

# The predicates are repeated from `_DOOMED` rather than trusted from it. This is
# the statement that can destroy data no feed can restore, and `sh.tmdb_id IS NOT
# NULL` here is what makes "a season under a locally-authored show is untouched"
# structural instead of a property of whichever query built the work list.
_DELETE = text("""
    DELETE FROM catalog.season s
     USING catalog.show sh
     WHERE s.id = ANY(cast(:doomed AS bigint[]))
       AND sh.id = s.show_id
       AND s.tmdb_id IS NULL
       AND sh.tmdb_id IS NOT NULL
""")

_COUNTS = text(f"""
    WITH doomed AS ({_DOOMED}),
         copied AS (
             SELECT s.id, s.show_id, s.season_number, sh.tmdb_id AS show_tmdb_id
               FROM catalog.season s
               JOIN catalog.show sh ON sh.id = s.show_id
              WHERE s.tmdb_id IS NULL
         )
    SELECT (SELECT count(*) FROM doomed) AS duplicates,
           (SELECT count(*) FROM catalog.episode e
             WHERE e.season_id IN (SELECT doomed_id FROM doomed)) AS episodes_to_repoint,
           (SELECT count(*) FROM catalog.episode e
             WHERE e.season_id IN (SELECT doomed_id FROM doomed)
               AND (EXISTS (SELECT 1 FROM app.user_episode_watch w WHERE w.episode_id = e.id)
                 OR EXISTS (SELECT 1 FROM app.user_episode_rating r WHERE r.episode_id = e.id))
           ) AS episodes_carrying_user_data,
           (SELECT count(*) FROM copied WHERE show_tmdb_id IS NULL) AS kept_under_unmatched_show,
           (SELECT count(*) FROM copied c
             WHERE c.show_tmdb_id IS NOT NULL
               AND NOT EXISTS (SELECT 1 FROM catalog.season t
                                WHERE t.show_id = c.show_id
                                  AND t.season_number = c.season_number
                                  AND t.tmdb_id IS NOT NULL)) AS kept_no_counterpart,
           (SELECT count(*) FROM copied c
             WHERE c.show_tmdb_id IS NOT NULL
               AND (SELECT count(*) FROM catalog.season t
                     WHERE t.show_id = c.show_id
                       AND t.season_number = c.season_number
                       AND t.tmdb_id IS NOT NULL) > 1) AS ambiguous
""")

# Every `(show, season number)` that would still carry more than one row once the
# pass has run — the complete residue of the first acceptance criterion, which
# this pass does not fully reach. Reported rather than left for someone to
# rediscover, and deliberately *not* scoped to unmatched shows: all three ways a
# pair survives belong here, and only the first was obvious.
#
# * `show_matched` false — TV Maze numbers two seasons the same on 13 shows
#   (NEU-1042 carried 2,298 such triples across), and where that show never
#   matched TMDB both rows are locally-authored. The second criterion — a season
#   under a locally-authored show is untouched — wins. Nine pairs in production.
# * `show_matched` true, `ingested_rows` 0 — the same TV Maze duplicate under a
#   show that *did* match, on a number TMDB has no season for. Neither row has a
#   counterpart to defer to, so `_DOOMED` exempts both. Thirty-three pairs in
#   production, and scoping this query to unmatched shows hid every one of them
#   inside `kept_no_counterpart`.
# * `ingested_rows` above 1 — two rows the ingest itself wrote for one number,
#   which is the ambiguity `_DOOMED` refuses. Those violate the criterion on
#   their own, before any copied row is counted. None in production.
_STILL_DOUBLED = text(f"""
    WITH doomed AS ({_DOOMED})
    SELECT s.show_id,
           s.season_number,
           count(*) AS rows,
           count(*) FILTER (WHERE s.tmdb_id IS NOT NULL) AS ingested_rows,
           bool_or(sh.tmdb_id IS NOT NULL) AS show_matched
      FROM catalog.season s
      JOIN catalog.show sh ON sh.id = s.show_id
     WHERE NOT EXISTS (SELECT 1 FROM doomed d WHERE d.doomed_id = s.id)
     GROUP BY s.show_id, s.season_number
    HAVING count(*) > 1
     ORDER BY count(*) DESC, s.show_id, s.season_number
""")


class SeasonDedupeAborted(Exception):
    """A batch did not delete what it selected. The message is what to read."""


@dataclass(frozen=True)
class DedupeResult:
    """What one run of the pass actually did."""

    seasons_deleted: int
    episodes_repointed: int
    batches: int


@dataclass(frozen=True)
class DedupeReport:
    """The state of the season grain, as counts a person can act on.

    Flat and JSON-shaped for the same reason `human_queue`'s rows are: it is read
    in a terminal and piped over `ssh docker exec`.
    """

    # Deliberately not `duplicates`: this counts the ones the pass can *act* on,
    # and reaching zero is not the same as the season grain being clean. What is
    # left over is in `still_doubled`, which is the criterion's real scoreboard.
    deletable_duplicates: int
    episodes_to_repoint: int
    episodes_carrying_user_data: int
    kept_under_unmatched_show: int
    kept_no_counterpart: int
    ambiguous: int
    still_doubled: tuple[dict[str, int | bool], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "deletable_duplicates": self.deletable_duplicates,
            "episodes_to_repoint": self.episodes_to_repoint,
            "episodes_carrying_user_data": self.episodes_carrying_user_data,
            "kept_under_unmatched_show": self.kept_under_unmatched_show,
            "kept_no_counterpart": self.kept_no_counterpart,
            "ambiguous": self.ambiguous,
            "still_doubled": [dict(row) for row in self.still_doubled],
        }


async def dedupe_seasons(
    db: AsyncSession,
    *,
    limit: int | None = None,
    batch_size: int = BATCH_SIZE,
) -> DedupeResult:
    """Re-point then delete every superseded copied season, a batch per transaction.

    `limit` caps how many seasons the run deletes, which is how to try a hundred
    before spending the full pass. Idempotent and resumable: a row leaves the work
    list by being deleted, so a re-run costs only what is genuinely still there.
    """
    seasons_deleted = 0
    episodes_repointed = 0
    batches = 0

    while True:
        size = batch_size if limit is None else min(batch_size, limit - seasons_deleted)
        if size <= 0:
            break

        rows = (await db.execute(_SELECT_BATCH, {"limit": size})).all()
        if not rows:
            break

        doomed = [row.doomed_id for row in rows]
        survivors = [row.survivor_id for row in rows]

        repointed = await db.execute(_REPOINT, {"doomed": doomed, "survivors": survivors})
        deleted = await db.execute(_DELETE, {"doomed": doomed})

        # `_DELETE` re-derives the predicates `_SELECT_BATCH` used, so disagreeing
        # with it means the two queries disagree about what is safe to delete —
        # and since the work list would then hand back the same rows next time
        # round, the loop would spin rather than fail. Stop instead, with the
        # batch rolled back.
        if deleted.rowcount != len(doomed):  # type: ignore[attr-defined]
            await db.rollback()
            raise SeasonDedupeAborted(
                f"selected {len(doomed)} season(s) to delete but the delete matched "
                f"{deleted.rowcount}; refusing to continue"  # type: ignore[attr-defined]
            )

        await db.commit()

        seasons_deleted += len(doomed)
        episodes_repointed += repointed.rowcount  # type: ignore[attr-defined]
        batches += 1
        log.info(
            "batch %d: re-pointed %d episode(s), deleted %d season(s) (%d total)",
            batches,
            repointed.rowcount,  # type: ignore[attr-defined]
            len(doomed),
            seasons_deleted,
        )

    return DedupeResult(
        seasons_deleted=seasons_deleted,
        episodes_repointed=episodes_repointed,
        batches=batches,
    )


async def build_report(db: AsyncSession) -> DedupeReport:
    """Count what the pass would do and what it is deliberately leaving alone.

    Needs no TMDB credential and writes nothing — safe to run against production
    before deciding to spend the pass, and the thing to re-read afterwards to
    confirm `deletable_duplicates` reached zero — and to read `still_doubled`,
    which is what the first acceptance criterion actually scores against.
    """
    counts = (await db.execute(_COUNTS)).one()
    residue = (await db.execute(_STILL_DOUBLED)).all()
    return DedupeReport(
        deletable_duplicates=counts.duplicates,
        episodes_to_repoint=counts.episodes_to_repoint,
        episodes_carrying_user_data=counts.episodes_carrying_user_data,
        kept_under_unmatched_show=counts.kept_under_unmatched_show,
        kept_no_counterpart=counts.kept_no_counterpart,
        ambiguous=counts.ambiguous,
        still_doubled=tuple(
            {
                "show_id": row.show_id,
                "season_number": row.season_number,
                "rows": row.rows,
                "ingested_rows": row.ingested_rows,
                "show_matched": row.show_matched,
            }
            for row in residue
        ),
    )
