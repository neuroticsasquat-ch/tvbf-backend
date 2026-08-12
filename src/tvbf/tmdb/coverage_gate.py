"""The pre-cutover go/no-go: is `catalog` safe to repoint the app onto? (NEU-1048)

The last check **before the cutover window opens**. Everything after it is
irreversible in the sense that matters — NEU-1046 swaps five foreign keys onto
`catalog`, and from that point on a missing row is a user's history pointing at
nothing rather than a row a re-run can put back.

Two things live here and they are deliberately different in kind.

## The gate has teeth; the coverage comparison does not

**The criteria are hard.** Each one is a way the cutover breaks user data, and
each fails the run. They are written down in `docs/migration/README.md` and in
`_CRITERIA` below, and they were fixed *before* the report was first run — a gate
whose failure conditions are chosen after seeing the numbers is not a gate.

**The language/era breakdown is a measurement, not a criterion.** It answers the
one risk ADR-0007 accepted without measuring — *"228,611 > 88,971 is a count, not
a guarantee TMDB holds our 4,536 Russian and 3,243 Chinese entries"* — and the
answer is now known to be *no*: NEU-1066's prune deliberately dropped 26,141
unmatched copied shows, 4,898 Russian and 2,326 Chinese among them, on the rule
that **the catalog is TMDB plus the shows users have history on**. Breadth from
the source being retired is not carried.

So a breadth threshold here would fail by construction, against a decision the
project has already taken and merged. The project spec says the same thing in
one line: *"a catalog comparison ... catches a long-tail regression before the
window. It is a safety check, not a decision gate — the decision is made."* The
breakdown is therefore reported with an **advisory** flag on the worst buckets,
and the JSON is deterministic so a re-run diffs against the last one. A bucket
that gets *worse* between two runs is the regression this exists to catch; a
bucket that is thin today is the accepted cost.

## What "covered" means once the prune has run

A TV Maze show is measured against `catalog` three ways, and only the third is a
genuine loss:

* **carried** — a `catalog.show` row still stands under its preserved TV Maze id.
  `/shows/:id` resolves and every `app` row pointing at it survives NEU-1046.
* **dropped, with a title twin** — the copy is gone, but an ingested TMDB row
  carries the same folded title. The show is in the catalog under a different id;
  what was lost is the id, and only for a show nobody had touched (the prune's
  own predicate).
* **dropped, without a title twin** — no ingested row shares the title. This is
  breadth TMDB does not appear to hold, and it is what the language and era
  buckets are counting.

The title comparison is folded through Postgres via `sql_fold`, the same single
definition browse search and NEU-1043's matching use, and it is an outer join
rather than a correlated `EXISTS` for the reason `show_prune` gives: the planner
hashes both folded sides once instead of folding 228,000 names 26,000 times.
Its limit is `show_prune`'s limit — exact folded equality cannot see a grain
mismatch, so "Cunk on Earth" against TMDB's "Cunk on..." counts as absent. That
biases the measurement toward *over*-reporting loss, which is the right direction
for a safety check.

## Why it needs `tvmaze` to still be standing

The denominator is `tvmaze.show` — the 88,971 rows the migration started from.
That is only readable while the schema lives, which it does until NEU-1051. Run
this before that ticket, or the comparison has nothing to compare against.

## Exit codes

`0` go, `1` no-go, `2` the gate could not run. The third is separate on purpose:
a crashed gate must never be filed as a considered verdict, and both non-zero
codes fail closed.
"""

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Text, and_, func, literal, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql import Select
from sqlalchemy.sql.elements import ColumnElement

from tvbf.catalog import models as m
from tvbf.sql_fold import folded
from tvbf.tmdb.human_queue import TOUCHED_SHOWS
from tvbf.tmdb.show_prune import MIN_INGESTED_SHOWS
from tvbf.tvmaze.models import Show as MazeShow

log = logging.getLogger(__name__)

# The criteria, in the order they are reported. Written down here rather than
# left implicit in the checks, because "no-go criteria decided in advance" is an
# acceptance criterion of this ticket and a docstring is where it survives.
_CRITERIA: tuple[tuple[str, str], ...] = (
    (
        "fk_targets_resolve",
        "Every id in the five columns NEU-1046 repoints resolves against catalog",
    ),
    ("user_touched_shows_present", "Every show a user has touched has a catalog.show row"),
    ("user_touched_shows_resolved", "Every show a user has touched has reached a verified mapping"),
    ("ingest_present", f"At least {MIN_INGESTED_SHOWS:,} shows carry the full ingest's watermark"),
)

# The two production rows that are unresolved *and* accepted, with the ruling
# that accepted them. Enumerated by hand for the reason `show_prune`'s README
# section enumerates them: neither is a locally-authored row, so neither can be
# expressed as a predicate — both are duplicates whose user history has to move
# onto an ingested id, and `queue:confirm` cannot do that post-ingest because
# `uq_show_tmdb_id` refuses. `neu-1066-user-touched-remediation.sql` resolves
# both, and it cannot run until NEU-1046 has repointed the foreign keys — which
# is this gate's own downstream ticket, so the exemption is what stops a known,
# sequenced remediation from reading as a fresh discovery.
#
# The list is an *exemption*, never an assertion: a row on it that no longer
# needs exempting simply stops appearing. A user-touched row that is not on it
# is a no-go, which is where the teeth are.
ACCEPTED_UNRESOLVED: dict[int, str] = {
    87519: (
        "Discretion — a plain duplicate of TMDB 300966, ingested as catalog id "
        "1202502; remediation queued behind NEU-1046 (NEU-1066)"
    ),
    63900: (
        "Cunk on Earth — TMDB models it as season 2 of 'Cunk on...' (TMDB 79063, "
        "ingested as catalog id 1067768), so no show-grain match exists; "
        "remediation queued behind NEU-1046 (NEU-1066)"
    ),
}

# A language or era bucket smaller than this is not evidence of anything — TV
# Maze carries a long tail of languages with a handful of shows each, and a
# bucket of four that lost three is noise, not a regression.
ADVISORY_MIN_BUCKET = 500

# Advisory only. Set at half the bucket because that is where "TMDB does not
# really hold this language" stops being arguable, not because anything happens
# at 50%.
ADVISORY_ABSENT_PCT = 50.0

_FK_DANGLING = text("""
    SELECT (SELECT count(*) FROM app.user_show_watch w
             WHERE NOT EXISTS (SELECT 1 FROM catalog.show s
                                WHERE s.id = w.show_id)) AS user_show_watch,
           (SELECT count(*) FROM app.user_show_rating r
             WHERE NOT EXISTS (SELECT 1 FROM catalog.show s
                                WHERE s.id = r.show_id)) AS user_show_rating,
           (SELECT count(*) FROM app.user_episode_watch w
             WHERE NOT EXISTS (SELECT 1 FROM catalog.episode e
                                WHERE e.id = w.episode_id)) AS user_episode_watch,
           (SELECT count(*) FROM app.user_episode_rating r
             WHERE NOT EXISTS (SELECT 1 FROM catalog.episode e
                                WHERE e.id = r.episode_id)) AS user_episode_rating
""")

# `import_ne` is created by the Next Episode import itself — not by `db:init`,
# not by a migration, and not by the test suite's conftest. Postgres resolves
# every relation in a statement at plan time, so this cannot be folded into the
# query above behind a CASE: it has to be asked for separately, and only after
# `to_regclass` says the table is there. NEU-1046 repoints it all the same.
_HAS_SHOW_RESOLUTION = text("SELECT to_regclass('import_ne.show_resolution') IS NOT NULL")

_SHOW_RESOLUTION_DANGLING = text("""
    SELECT count(*) FROM import_ne.show_resolution r
     WHERE NOT EXISTS (SELECT 1 FROM catalog.show s WHERE s.id = r.show_id)
""")

# A user-touched show with no `catalog.show` row at all. `app.activity_event` is
# polymorphic with no foreign key, so it is the one path here that the criterion
# above cannot see — it neither blocks nor cascades, it silently orphans, which
# is exactly why the reconciliation harness counts its rows explicitly.
_UNMIRRORED = text(f"""
    WITH touched AS ({TOUCHED_SHOWS})
    SELECT DISTINCT t.show_id
      FROM touched t
     WHERE t.show_id IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM catalog.show s WHERE s.id = t.show_id)
     ORDER BY t.show_id
""")

# A user-touched show carrying no `tmdb_id` and no human ruling. `match_method =
# 'human'` with a NULL id is a person's verdict that TMDB has no counterpart
# (NEU-1044) — a resolution, not a gap.
_UNRESOLVED = text(f"""
    WITH touched AS ({TOUCHED_SHOWS})
    SELECT s.id,
           s.name,
           s.match_method,
           (SELECT count(*) FROM app.user_episode_watch w
             JOIN tvmaze.episode e ON e.id = w.episode_id
            WHERE e.show_id = s.id) AS episode_watches
      FROM catalog.show s
     WHERE s.tmdb_id IS NULL
       AND s.match_method IS DISTINCT FROM 'human'
       AND EXISTS (SELECT 1 FROM touched t WHERE t.show_id = s.id)
     ORDER BY s.id
""")

_INGESTED_COUNT = text("SELECT count(*) FROM catalog.show WHERE tmdb_synced_at IS NOT NULL")


@dataclass(frozen=True)
class Criterion:
    """One hard rule, its verdict, and the evidence behind it."""

    name: str
    description: str
    passed: bool
    detail: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "passed": self.passed,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class Bucket:
    """One language or era slice of the TV Maze catalog, measured against `catalog`."""

    bucket: str
    tvmaze_shows: int
    carried: int
    carried_matched: int
    dropped: int
    dropped_with_title_twin: int
    dropped_without_title_twin: int

    @property
    def absent_pct(self) -> float:
        """Share of the bucket that is gone and has no ingested row by title."""
        if not self.tvmaze_shows:
            return 0.0
        return round(100.0 * self.dropped_without_title_twin / self.tvmaze_shows, 2)

    @property
    def advisory(self) -> bool:
        return self.tvmaze_shows >= ADVISORY_MIN_BUCKET and self.absent_pct >= ADVISORY_ABSENT_PCT

    def to_dict(self) -> dict[str, Any]:
        return {
            "bucket": self.bucket,
            "tvmaze_shows": self.tvmaze_shows,
            "carried": self.carried,
            "carried_matched": self.carried_matched,
            "dropped": self.dropped,
            "dropped_with_title_twin": self.dropped_with_title_twin,
            "dropped_without_title_twin": self.dropped_without_title_twin,
            "absent_pct": self.absent_pct,
            "advisory": self.advisory,
        }


@dataclass(frozen=True)
class GateReport:
    """The whole verdict: four hard criteria, plus the coverage measurement.

    Flat and JSON-shaped for the reason every other migration report here is: it
    is read in a terminal and piped over `ssh 'docker exec ...'`, because `docs/`
    is not in the production image.
    """

    criteria: tuple[Criterion, ...]
    by_language: tuple[Bucket, ...]
    by_era: tuple[Bucket, ...]

    @property
    def failed(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.criteria if not c.passed)

    @property
    def verdict(self) -> str:
        return "no-go" if self.failed else "go"

    @property
    def advisory_buckets(self) -> tuple[str, ...]:
        return tuple(b.bucket for b in self.by_language if b.advisory)

    def _totals(self) -> dict[str, int]:
        """Catalog-wide coverage, summed from the language buckets.

        Summed rather than re-queried so the totals and the breakdown cannot
        disagree — every TV Maze show lands in exactly one language bucket.
        """
        fields = (
            "tvmaze_shows",
            "carried",
            "carried_matched",
            "dropped",
            "dropped_with_title_twin",
            "dropped_without_title_twin",
        )
        return {f: sum(getattr(b, f) for b in self.by_language) for f in fields}

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "failed": list(self.failed),
            "criteria": [c.to_dict() for c in self.criteria],
            "coverage": {
                "totals": self._totals(),
                "advisory_languages": list(self.advisory_buckets),
                "by_language": [b.to_dict() for b in self.by_language],
                "by_era": [b.to_dict() for b in self.by_era],
            },
        }


def _language_bucket() -> ColumnElement[str]:
    """TV Maze's own language name — "English", "Russian" — or `(unknown)`.

    Deliberately `tvmaze.show.language` and not `catalog.show.original_language`:
    the denominator is the catalog being left, so a show that no longer has a
    `catalog` row still has to land in a bucket. TV Maze stores a name where TMDB
    stores an ISO code, and the names are what the ticket's numbers are quoted in.
    """
    return func.coalesce(MazeShow.language, literal("(unknown)", Text))


def _era_bucket() -> ColumnElement[str]:
    """The premiere decade as `1990s`, or `(unknown)` for no premiere date.

    `||` rather than `concat()`: Postgres's `concat()` treats a NULL argument as
    the empty string, so a show with no premiere date would come out as the
    bucket `"s"` and the coalesce below would never fire.
    """
    decade = func.to_char(func.date_trunc(literal("decade"), MazeShow.premiered), "YYYY")
    return func.coalesce(decade.concat(literal("s", Text)), literal("(unknown)", Text))


def _coverage_stmt(bucket: ColumnElement[str]) -> Select:
    """Measure every TV Maze show against `catalog`, grouped by `bucket`.

    Two joins and both are outer. The first asks whether the copy survives — a
    `catalog.show` under the preserved id, which is what makes `/shows/:id` still
    resolve. The second only fires for rows where it did not, and asks whether an
    *ingested* row carries the same folded title, which is the difference between
    "the show moved id" and "TMDB does not have this show".

    Grouping happens twice: once per show to collapse the twin join (a title can
    match several ingested rows), then once per bucket.
    """
    carried = aliased(m.Show)
    ingested = aliased(m.Show)
    per_show = (
        select(
            MazeShow.id.label("id"),
            bucket.label("bucket"),
            carried.id.is_not(None).label("carried"),
            carried.tmdb_id.is_not(None).label("matched"),
            func.count(ingested.id).label("twins"),
        )
        .select_from(MazeShow)
        .outerjoin(carried, carried.id == MazeShow.id)
        .outerjoin(
            ingested,
            and_(
                carried.id.is_(None),
                ingested.tmdb_id.is_not(None),
                folded(ingested.name) == folded(MazeShow.name),
                # A title that folds away to nothing ("!!!", "???") would
                # otherwise pair with every other such title — the same guard
                # `folded_equal` and `show_prune` carry, for the same reason.
                folded(MazeShow.name) != literal("", Text),
            ),
        )
        .group_by(MazeShow.id, bucket, carried.id, carried.tmdb_id)
        .subquery()
    )
    is_carried = per_show.c.carried.is_(True)
    is_dropped = per_show.c.carried.is_(False)
    return (
        select(
            per_show.c.bucket,
            func.count().label("tvmaze_shows"),
            func.count().filter(is_carried).label("carried"),
            func.count().filter(per_show.c.matched.is_(True)).label("carried_matched"),
            func.count().filter(is_dropped).label("dropped"),
            func.count().filter(and_(is_dropped, per_show.c.twins > 0)).label("with_twin"),
            func.count().filter(and_(is_dropped, per_show.c.twins == 0)).label("without_twin"),
        )
        .group_by(per_show.c.bucket)
        # Ordered by bucket name, not by size: the artifact is diffed against the
        # previous run, and a row that moves because a count changed turns every
        # later row into diff noise.
        .order_by(per_show.c.bucket)
    )


async def _buckets(db: AsyncSession, bucket: ColumnElement[str]) -> tuple[Bucket, ...]:
    rows = (await db.execute(_coverage_stmt(bucket))).all()
    return tuple(
        Bucket(
            bucket=row.bucket,
            tvmaze_shows=row.tvmaze_shows,
            carried=row.carried,
            carried_matched=row.carried_matched,
            dropped=row.dropped,
            dropped_with_title_twin=row.with_twin,
            dropped_without_title_twin=row.without_twin,
        )
        for row in rows
    )


async def _fk_targets_resolve(db: AsyncSession) -> Criterion:
    """G1 — the precondition NEU-1046's `ALTER TABLE` will enforce anyway.

    Asked here so it is answered while a dangling id is still a report line
    rather than a failed migration halfway through the window. `import_ne` is
    checked only when it exists; a missing schema is reported as such rather than
    silently counted as zero, since "no dangling rows" and "did not look" are the
    two answers a gate must never conflate.
    """
    counts = (await db.execute(_FK_DANGLING)).one()
    detail: dict[str, Any] = {
        "app.user_show_watch.show_id": counts.user_show_watch,
        "app.user_show_rating.show_id": counts.user_show_rating,
        "app.user_episode_watch.episode_id": counts.user_episode_watch,
        "app.user_episode_rating.episode_id": counts.user_episode_rating,
    }
    dangling = sum(detail.values())

    if (await db.execute(_HAS_SHOW_RESOLUTION)).scalar_one():
        resolution = (await db.execute(_SHOW_RESOLUTION_DANGLING)).scalar_one()
        detail["import_ne.show_resolution.show_id"] = resolution
        dangling += resolution
    else:
        detail["import_ne.show_resolution.show_id"] = "(schema absent — not checked)"

    return Criterion(
        name="fk_targets_resolve",
        description=_CRITERIA[0][1],
        passed=dangling == 0,
        detail=detail,
    )


async def _user_touched_shows_present(db: AsyncSession) -> Criterion:
    """G2 — a show somebody tracks with no catalog row at all."""
    missing = list((await db.execute(_UNMIRRORED)).scalars())
    return Criterion(
        name="user_touched_shows_present",
        description=_CRITERIA[1][1],
        passed=not missing,
        detail={"missing_show_ids": missing},
    )


async def _user_touched_shows_resolved(db: AsyncSession) -> Criterion:
    """G3 — the human queue's acceptance criterion, asked one last time.

    An unresolved row that is on `ACCEPTED_UNRESOLVED` is reported and does not
    fail the gate; anything else does. That split is the whole point: the two
    known rows are a sequenced remediation waiting on NEU-1046, and a third row
    appearing would be a show whose history nobody has looked at.
    """
    rows = (await db.execute(_UNRESOLVED)).all()
    accepted: list[dict[str, Any]] = []
    unaccepted: list[dict[str, Any]] = []
    for row in rows:
        entry: dict[str, Any] = {
            "show_id": row.id,
            "name": row.name,
            "match_method": row.match_method,
            "episode_watches": row.episode_watches,
        }
        if row.id in ACCEPTED_UNRESOLVED:
            accepted.append(entry | {"accepted_because": ACCEPTED_UNRESOLVED[row.id]})
        else:
            unaccepted.append(entry)

    return Criterion(
        name="user_touched_shows_resolved",
        description=_CRITERIA[2][1],
        passed=not unaccepted,
        detail={"unresolved": unaccepted, "accepted_exceptions": accepted},
    )


async def _ingest_present(db: AsyncSession, floor: int) -> Criterion:
    """G4 — without the ingest, every measurement below it is about a half-built catalog.

    Same floor and same device as `show_prune`'s `IngestNotRun` guard and the
    tombstone's plausibility check: a partial upstream picture read as
    authoritative is the shape of accident all three sit between.
    """
    ingested = (await db.execute(_INGESTED_COUNT)).scalar_one()
    return Criterion(
        name="ingest_present",
        description=_CRITERIA[3][1],
        passed=ingested >= floor,
        detail={"ingested_shows": ingested, "floor": floor},
    )


async def build_gate_report(
    db: AsyncSession,
    *,
    min_ingested: int = MIN_INGESTED_SHOWS,
) -> GateReport:
    """Run every criterion and the coverage comparison. Writes nothing.

    `min_ingested` is the floor guard's threshold, lowered only by tests — seeding
    150,000 synced shows to exercise four criteria is not a trade worth making.
    Nothing in production passes it, so the default is the guard.
    """
    criteria = (
        await _fk_targets_resolve(db),
        await _user_touched_shows_present(db),
        await _user_touched_shows_resolved(db),
        await _ingest_present(db, min_ingested),
    )
    return GateReport(
        criteria=criteria,
        by_language=await _buckets(db, _language_bucket()),
        by_era=await _buckets(db, _era_bucket()),
    )
