"""Retire the copied show rows the migration could not map and nobody tracks (NEU-1066).

NEU-1042 copied every `tvmaze.show` into `catalog.show` with `tmdb_id IS NULL`
and its id preserved. NEU-1043's enrichment attached a `tmdb_id` to 62,882 of
them. NEU-1034's ingest then mirrored TMDB's whole catalog — and because every
`catalog` upsert conflict-targets `tmdb_id` (ADR-0008) and **Postgres treats
NULLs as distinct in a unique index**, the two populations behaved oppositely:

* a copied row *holding* a `tmdb_id` conflicted, so the TMDB payload landed on
  that same row — one row, TMDB data, preserved id, nothing duplicated;
* a copied row with `tmdb_id IS NULL` could never conflict with anything, so
  TMDB's series was inserted **beside** it under a fresh surrogate.

So `catalog` carries two rows for one show wherever the matcher failed and TMDB
has the series anyway — id 10158 holding TV Maze's "ITV News at Ten" and id
1003587 holding TMDB's, each with its own disjoint tree of seasons and episodes.
This is the show grain of the problem NEU-1045 pre-empted at episode grain (map
before the ingest, so the rows merge) and NEU-1119 cleaned at season grain (delete
the copy, because the ingest has already run and `uq_season_tmdb_id` refuses the
cheap mapping). The same wall stands here: `uq_show_tmdb_id` means a copied row
can no longer be handed the id its ingested twin already holds.

## Why this deletes every unmatched copy, not just the demonstrated duplicates

The ticket assumed an unmatched row is ambiguous between "TMDB has it, we failed
to match" (a duplicate) and "TMDB does not have it" (a locally-authored row
ADR-0008 sanctions), with no way to tell them apart — so it proposed hiding
duplicates from discovery rather than deleting them. That reasoning was written
before the ingest ran, and the ingest is what dissolves it: TMDB's whole catalog
is now local, so the question is answerable in SQL, and it was answered against
production on 2026-08-11. Of 26,143 unmatched rows, 6,464 share a folded title
with an ingested row and 3,337 also agree on first-air year to within one — so
roughly three quarters of them duplicate nothing at all.

That measurement is what makes the *simpler* rule the right one rather than a
blunt one. **The catalog is TMDB, plus the shows users have history on.**
Locally-authored rows exist to keep the no-loss guarantee (project spec, "Human
matching queue": *"Where TMDB genuinely lacks a show, the fallback is a
locally-authored row"*), not to preserve TV Maze's catalog breadth — and TV Maze
is the source being retired. So an unmatched copy that no user has ever touched
is carried for nobody: it is either a duplicate, or breadth from a source we are
leaving. Both go.

Worth knowing before reading an unmatched row as evidence TMDB lacks something:
**"absent from TMDB" is usually a grain mismatch.** Production's two user-touched
unmatched rows were both checked against the live API on 2026-08-11 and neither
was absent. "Discretion" is a plain duplicate (TMDB 300966). "Cunk on Earth" is a
*show* to TV Maze and **season 2 of "Cunk on..." (TMDB 79063)** to TMDB — so
`/find` by its tvdb id returns nothing and tier 3's search returns four results
with no exact title match, and both tiers decline correctly. Neither is a case
for a locally-authored row; both are cases for moving user history by hand.

The cost is stated rather than discovered: production drops 26,141 shows,
including 4,898 Russian and 2,326 Chinese entries — the long tail the project
spec flagged as unproven. 2,406 of them have no episodes at all.

## The four predicates, and why the DELETE repeats every one

`_DOOMED` is the work list and `_DELETE` re-derives it, for the reason
`season_dedupe` does: this is the statement that destroys rows, so what it spares
must be structural rather than a property of whichever query selected the batch.

* **`tmdb_id IS NULL`** — a matched row is not a copy any more, it *is* the show.
* **not user-touched** — the whole point, and it reuses `human_queue`'s
  `TOUCHED_SHOWS` rather than growing a second definition. That union already
  resolves `app.activity_event` per target type, which matters because it is
  polymorphic with no foreign key at all: it neither blocks a delete nor
  cascades, it just silently orphans (ADR-0005 cites exactly this hazard).
* **`match_method IS DISTINCT FROM 'human'`** — that value with a NULL `tmdb_id`
  is a person's verdict that TMDB has no counterpart, so the row is
  locally-authored *on purpose* (NEU-1044). Deleting it would throw away the
  review it records and put the show back in the queue.
* **the row still exists in `tvmaze.show`** — "delete the copy" has to mean the
  copy. A future locally-authored row also reads `tmdb_id IS NULL`, and this is
  what keeps it out of reach; it is also what makes the revert exact, since
  `task copy:catalog` restores precisely the rows that pass this test.

## What the delete takes with it, and what it cannot reach

`catalog.season` and `catalog.episode` cascade from `catalog.show`, so a doomed
show takes its own seasons and episodes (47,443 and 840,169 in production).
Nothing else rides along: the copy wrote only `show`, `season`, `episode` and
`show_aka`, so a copied show has no credits, images or genre rows.

Nothing in `app` references `catalog` yet — the repoint is NEU-1046 — so today
this delete cannot reach user data even in principle. That is a coincidence of
timing and not the safeguard; the touched predicate is. After NEU-1046 the same
predicate is what keeps a cascade from ever having something to cascade *to*.

## Reversible while `tvmaze` stands

`task copy:catalog` restores every deleted row under its original id, with its
seasons and episodes, because the copy is idempotent and id-preserving and
`tvmaze` is not dropped until NEU-1051. Unlike the season grain, the revert is a
single command: this pass re-points nothing, so there is no second statement to
repair parentage. What it does *not* restore is a `tmdb_id`, which is correct —
these rows never had one.

## The floor guard, and what it is actually protecting against

Run before `enrich:tmdb-ids`, this pass's work list is every copied show —
all 89,025 of them, since none holds a `tmdb_id` yet. Reversible or not, that is
not a mistake to make silently at 3am, so `prune_shows` refuses unless at least
`MIN_INGESTED_SHOWS` rows carry the ingest's own watermark. Same device and same
floor as the tombstone's plausibility check, guarding the same shape of accident:
an empty or partial upstream picture read as authoritative.
"""

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import BigInteger, Text, and_, func, literal, literal_column, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql import Select

from tvbf.catalog import models as m
from tvbf.sql_fold import folded
from tvbf.tmdb.human_queue import TOUCHED_SHOWS

log = logging.getLogger(__name__)

# Shows per transaction. Deliberately an order of magnitude below
# `season_dedupe.BATCH_SIZE`, because a show here drags its whole tree behind it:
# at the measured ~32 episodes per doomed show a batch of 100 cascades ~3,200
# episode rows, which is the same order of work per transaction as that pass's
# 500 seasons. The ids travel as one array rather than a bind per row, so
# Postgres's 32,767-parameter cap never enters into it.
BATCH_SIZE = 100

# The ingest mirrored 228,841 series in production. This floor is the tombstone's
# `_MIN_FEED_ABSOLUTE` and is set the same way: comfortably below a real catalog,
# far above anything a partial pass would leave behind.
MIN_INGESTED_SHOWS = 150_000

# A copied show the migration never mapped, that no user has touched, that no
# person has ruled locally-authored, and that is still a copy. See the module
# docstring for why each predicate is here — and `_DELETE` for why it repeats
# all four rather than trusting this.
_DOOMED = f"""
    SELECT s.id
      FROM catalog.show s
     WHERE s.tmdb_id IS NULL
       AND s.match_method IS DISTINCT FROM 'human'
       AND EXISTS (SELECT 1 FROM tvmaze.show t WHERE t.id = s.id)
       AND NOT EXISTS (SELECT 1 FROM ({TOUCHED_SHOWS}) u WHERE u.show_id = s.id)
"""

_SELECT_BATCH = text(f"""
    {_DOOMED}
     ORDER BY s.id
     LIMIT :limit
""")

# Every predicate from `_DOOMED`, restated. A work list is a query result; this
# is the statement that destroys rows no feed can restore, so "a show a user has
# touched is untouchable" is asserted here rather than inherited.
_DELETE = text(f"""
    DELETE FROM catalog.show s
     WHERE s.id = ANY(cast(:doomed AS bigint[]))
       AND s.tmdb_id IS NULL
       AND s.match_method IS DISTINCT FROM 'human'
       AND EXISTS (SELECT 1 FROM tvmaze.show t WHERE t.id = s.id)
       AND NOT EXISTS (SELECT 1 FROM ({TOUCHED_SHOWS}) u WHERE u.show_id = s.id)
""")

_INGESTED_COUNT = text("SELECT count(*) FROM catalog.show WHERE tmdb_synced_at IS NOT NULL")

# The three kept populations are counted as they are *spared* — each is an
# unmatched row that fails exactly one of `_DOOMED`'s predicates — so the four
# numbers partition every `tmdb_id IS NULL` row and a reader can check that they
# add up.
_COUNTS = text(f"""
    WITH doomed AS ({_DOOMED})
    SELECT (SELECT count(*) FROM doomed) AS deletable,
           (SELECT count(*) FROM catalog.season s
             WHERE s.show_id IN (SELECT id FROM doomed)) AS seasons_to_delete,
           (SELECT count(*) FROM catalog.episode e
             WHERE e.show_id IN (SELECT id FROM doomed)) AS episodes_to_delete,
           (SELECT count(*) FROM catalog.show s
             WHERE s.tmdb_id IS NULL
               AND EXISTS (SELECT 1 FROM ({TOUCHED_SHOWS}) u
                            WHERE u.show_id = s.id)) AS kept_user_touched,
           (SELECT count(*) FROM catalog.show s
             WHERE s.tmdb_id IS NULL
               AND s.match_method = 'human'
               AND NOT EXISTS (SELECT 1 FROM ({TOUCHED_SHOWS}) u
                                WHERE u.show_id = s.id)) AS kept_human_verdict,
           (SELECT count(*) FROM catalog.show s
             WHERE s.tmdb_id IS NULL
               AND s.match_method IS DISTINCT FROM 'human'
               AND NOT EXISTS (SELECT 1 FROM tvmaze.show t WHERE t.id = s.id)
               AND NOT EXISTS (SELECT 1 FROM ({TOUCHED_SHOWS}) u
                                WHERE u.show_id = s.id)) AS kept_not_copied
""")

# The shows the pass spares because a user touched them, with the evidence a
# person needs to act: how much history hangs off each, and whether an ingested
# row shares its title. Both matter — an unmatched row with a title twin is a
# duplicate somebody must merge by hand (`queue:confirm` cannot do it post-ingest,
# `uq_show_tmdb_id` refuses), while one without is simply a show TMDB lacks and
# is finished business.
_USER_TOUCHED = text(f"""
    SELECT s.id,
           s.name,
           s.first_air_date,
           coalesce(s.match_method, '(unreviewed)') AS match_method,
           (SELECT count(*) FROM app.user_episode_watch w
             JOIN tvmaze.episode e ON e.id = w.episode_id
            WHERE e.show_id = s.id) AS episode_watches,
           EXISTS (SELECT 1 FROM app.user_show_watch t WHERE t.show_id = s.id) AS tracked
      FROM catalog.show s
     WHERE s.tmdb_id IS NULL
       AND EXISTS (SELECT 1 FROM ({TOUCHED_SHOWS}) u WHERE u.show_id = s.id)
     ORDER BY s.id
""")


class ShowPruneAborted(Exception):
    """A batch did not delete what it selected. The message is what to read."""


class IngestNotRun(Exception):
    """Too few ingested shows for the work list to mean what it claims.

    Raised rather than returning an empty result: a pass that quietly did
    nothing and a pass that quietly deleted 89,025 shows are the two failures
    this guard sits between, and only one of them is loud on its own.
    """


@dataclass(frozen=True)
class PruneResult:
    """What one run of the pass actually did."""

    shows_deleted: int
    batches: int


@dataclass(frozen=True)
class PruneReport:
    """The state of the show grain, as counts and rows a person can act on.

    Flat and JSON-shaped for the same reason `human_queue`'s rows are: it is read
    in a terminal and piped over `ssh docker exec`.
    """

    deletable: int
    deletable_with_title_twin: int
    deletable_without_title_twin: int
    seasons_to_delete: int
    episodes_to_delete: int
    kept_user_touched: int
    kept_human_verdict: int
    kept_not_copied: int
    user_touched: tuple[dict[str, Any], ...]
    still_doubled: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "deletable": self.deletable,
            "deletable_with_title_twin": self.deletable_with_title_twin,
            "deletable_without_title_twin": self.deletable_without_title_twin,
            "seasons_to_delete": self.seasons_to_delete,
            "episodes_to_delete": self.episodes_to_delete,
            "kept_user_touched": self.kept_user_touched,
            "kept_human_verdict": self.kept_human_verdict,
            "kept_not_copied": self.kept_not_copied,
            "user_touched": [dict(row) for row in self.user_touched],
            "still_doubled": [dict(row) for row in self.still_doubled],
        }


def _doomed_cte():
    """The work list as a CTE, for the two statements that must fold a title.

    `_DOOMED` is `text()` like everything else here, but a title comparison has
    to go through `sql_fold.folded` — so these two are built in Core and pull the
    work list in through this rather than re-spelling it.
    """
    return text(_DOOMED).columns(literal_column("id", BigInteger)).cte("doomed")


def _title_twin_split_stmt() -> Select:
    """Split the doomed rows by whether an ingested row shares their title.

    This is the half of the first acceptance criterion the partition counts do
    not answer: *"how many are genuinely absent from TMDB rather than merely
    unmatched"*. A doomed row with a title twin is a duplicate the matcher missed;
    one without is breadth from the source being retired. Production 2026-08-11:
    6,464 with, 19,679 without.

    An **outer join, not a correlated EXISTS** — the planner hashes both folded
    sides once, where a per-row subquery would fold 228,000 names 26,000 times.
    """
    doomed = _doomed_cte()
    ingested = aliased(m.Show)
    twins = (
        select(
            m.Show.id.label("id"),
            func.count(ingested.id).label("twins"),
        )
        .join(doomed, doomed.c.id == m.Show.id)
        .outerjoin(
            ingested,
            and_(
                ingested.tmdb_id.is_not(None),
                folded(ingested.name) == folded(m.Show.name),
                folded(m.Show.name) != literal("", Text),
            ),
        )
        .group_by(m.Show.id)
        .subquery()
    )
    return select(
        func.count().filter(twins.c.twins > 0).label("with_twin"),
        func.count().filter(twins.c.twins == 0).label("without_twin"),
    ).select_from(twins)


def _still_doubled_stmt() -> Select:
    """Kept unmatched rows that will still share a title with an ingested row.

    The scoreboard for *"no show appears twice in browse or search"* — the pass
    empties the work list, and this is what says whether the criterion is
    actually met. Anything here is a duplicate the pass deliberately spared, so
    in production it is the user-touched residue and nothing else.

    Built through SQLAlchemy rather than as another `text()` block because the
    title comparison has to go through `sql_fold.folded`. There is exactly one
    definition of the fold and it is that one; re-spelling
    `immutable_unaccent(lower(regexp_replace(...)))` here would be a second that
    silently disagrees with browse search on ł, ø, đ and ħ.
    """
    doomed = _doomed_cte()
    kept = aliased(m.Show)
    ingested = aliased(m.Show)
    return (
        select(kept.id, kept.name, func.array_agg(ingested.id).label("ingested_ids"))
        .join(
            ingested,
            and_(
                ingested.tmdb_id.is_not(None),
                folded(ingested.name) == folded(kept.name),
                # A title that folds away to nothing ("!!!", "???") would
                # otherwise report every other such title as its twin — the same
                # guard `folded_equal` carries, for the same reason.
                folded(kept.name) != literal("", Text),
            ),
        )
        .where(
            kept.tmdb_id.is_(None),
            kept.id.not_in(select(doomed.c.id)),
        )
        .group_by(kept.id, kept.name)
        .order_by(kept.id)
    )


async def _guard_ingest_ran(db: AsyncSession, floor: int) -> None:
    ingested = (await db.execute(_INGESTED_COUNT)).scalar_one()
    if ingested < floor:
        raise IngestNotRun(
            f"{ingested} show(s) carry a tmdb_synced_at, under the floor of "
            f"{floor} — run the full TMDB catalog ingest first, or every "
            f"copied show reads as unmatched and the work list is the whole mirror"
        )


async def prune_shows(
    db: AsyncSession,
    *,
    limit: int | None = None,
    batch_size: int = BATCH_SIZE,
    min_ingested: int = MIN_INGESTED_SHOWS,
) -> PruneResult:
    """Delete every unmatched, untouched copied show, a batch per transaction.

    `limit` caps how many shows the run deletes, which is how to try a hundred
    before spending the full pass. Idempotent and resumable: a row leaves the
    work list by being deleted, so a re-run costs only what is genuinely still
    there — and re-running it after a later ingest or delta is the point, since
    neither creates a copied row but both can turn one's twin into a match.

    `min_ingested` is the floor guard's threshold, lowered only by tests — seeding
    150,000 synced shows to exercise three is not a trade worth making. Nothing in
    production passes it, so the default is the guard.
    """
    await _guard_ingest_ran(db, min_ingested)

    shows_deleted = 0
    batches = 0

    while True:
        size = batch_size if limit is None else min(batch_size, limit - shows_deleted)
        if size <= 0:
            break

        doomed = list((await db.execute(_SELECT_BATCH, {"limit": size})).scalars())
        if not doomed:
            break

        deleted = await db.execute(_DELETE, {"doomed": doomed})

        # `_DELETE` re-derives the predicates `_SELECT_BATCH` used, so disagreeing
        # with it means the two queries disagree about what is safe to delete —
        # and since the work list would then hand back the same rows next time
        # round, the loop would spin rather than fail. Stop instead, with the
        # batch rolled back.
        if deleted.rowcount != len(doomed):  # type: ignore[attr-defined]
            await db.rollback()
            raise ShowPruneAborted(
                f"selected {len(doomed)} show(s) to delete but the delete matched "
                f"{deleted.rowcount}; refusing to continue"  # type: ignore[attr-defined]
            )

        await db.commit()

        shows_deleted += len(doomed)
        batches += 1
        log.info("batch %d: deleted %d show(s) (%d total)", batches, len(doomed), shows_deleted)

    return PruneResult(shows_deleted=shows_deleted, batches=batches)


async def build_report(db: AsyncSession) -> PruneReport:
    """Count what the pass would do and enumerate what it deliberately keeps.

    Needs no TMDB credential, writes nothing, and carries **no floor guard** —
    reading is how you find out whether the ingest has run, so refusing to
    report until it has would be backwards.
    """
    counts = (await db.execute(_COUNTS)).one()
    twins = (await db.execute(_title_twin_split_stmt())).one()
    touched = (await db.execute(_USER_TOUCHED)).all()
    doubled = (await db.execute(_still_doubled_stmt())).all()

    return PruneReport(
        deletable=counts.deletable,
        deletable_with_title_twin=twins.with_twin,
        deletable_without_title_twin=twins.without_twin,
        seasons_to_delete=counts.seasons_to_delete,
        episodes_to_delete=counts.episodes_to_delete,
        kept_user_touched=counts.kept_user_touched,
        kept_human_verdict=counts.kept_human_verdict,
        kept_not_copied=counts.kept_not_copied,
        user_touched=tuple(
            {
                "show_id": row.id,
                "name": row.name,
                "first_air_date": row.first_air_date.isoformat() if row.first_air_date else None,
                "match_method": row.match_method,
                "episode_watches": row.episode_watches,
                "tracked": row.tracked,
            }
            for row in touched
        ),
        still_doubled=tuple(
            {"show_id": row.id, "name": row.name, "ingested_ids": list(row.ingested_ids)}
            for row in doubled
        ),
    )
