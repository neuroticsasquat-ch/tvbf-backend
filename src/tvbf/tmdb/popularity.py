"""Refresh `catalog.show.popularity` from the daily id export (NEU-1172).

`/tv/changes` reports **content edits**, and TMDB recomputing a popularity score
is not one. Measured in production 2026-08-16: the nightly delta re-syncs
1,300–2,300 shows a day against a mirror of 229k, and 200,698 of those rows were
last synced on 2026-08-10. Popular shows stay fresh incidentally — a show being
watched is a show being edited — but the tail drifts without bound, and a ranked
list built on mixed-vintage scores is wrong in a way nothing surfaces.

The fix costs nothing, because the file is already downloaded: `export.py`
fetches the id export for the ingest's work list and for the tombstone reverse
diff, and every one of its 229,150 lines carries `popularity` beside `id`. No
request, no rate budget, no credential.

## Four rules, each of which this module would be wrong without

**It writes `popularity` and nothing else.** This is not an ingest and must not
become one. The export's third field, `original_name`, is not `catalog.show.name`
and is no substitute for one.

**It does not touch `tmdb_synced_at` or `credits_synced_at`.** A popularity score
arriving from the export is not evidence that a payload was mirrored, and
stamping either watermark would retire shows from a work list they belong on —
the same distinction NEU-1127 had to draw when it added a second watermark
rather than reuse the first.

**It matches on `tmdb_id`, never the primary key**, like every other `catalog`
write: `catalog.show.id` is an internal surrogate the migration seeded from TV
Maze's ids (ADR-0008) and means nothing to the export.

**It refuses an implausible export**, on the same floors the tombstone pass uses
(`export.feed_is_implausible`). A short file must not be allowed to skew the
popularity of the mirror any more than it may tombstone it — and this pass sees
the *same* file, so a partial download that cleared the guard here and failed it
there would leave the two disagreeing about what upstream holds.

## Why the write is `UPDATE ... FROM (VALUES …)`

229k rows against Postgres's 32,767 bind-parameter cap is the ceiling that
forces `_BATCH_SIZE` in `upsert.py` and the export diff being computed in Python
in `tombstone.py`. One statement per row is 229k round trips a night; one
statement for the lot cannot be bound. A batched `VALUES` join is neither.

The `IS DISTINCT FROM` clause is what makes a re-run genuinely free rather than
merely idempotent: without it every nightly pass rewrites all 229k rows and
their dead tuples, whether or not a single score moved.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import BigInteger, Double, column, func, select, update, values
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.catalog import models as m
from tvbf.tmdb.export import ExportEntry, feed_is_implausible

log = logging.getLogger(__name__)

# Two bind parameters a row against Postgres's 32,767 cap, so this is an order
# of magnitude inside it — sized for round trips (46 statements for the whole
# catalog) rather than against the ceiling.
_BATCH_SIZE = 5000


@dataclass(frozen=True)
class PopularityResult:
    """What one refresh did. `scored` is what the export offered, `updated` what
    actually moved — equal only on the first run against a stale mirror."""

    updated: int
    scored: int
    skipped_reason: str | None = None


def _update_batch(chunk: Sequence[ExportEntry]):
    """`UPDATE catalog.show SET popularity = … FROM (VALUES …) WHERE tmdb_id = …`.

    The columns are typed explicitly because a `VALUES` list is not a table:
    Postgres has nothing to infer from, and an untyped literal would resolve to
    `text` and then compare `bigint = text` — the same trap `watch_archive`'s
    placeholder columns document.
    """
    export = values(
        column("tmdb_id", BigInteger),
        column("popularity", Double),
        name="export",
    ).data([(e.tmdb_id, e.popularity) for e in chunk])
    return (
        update(m.Show)
        .where(m.Show.tmdb_id == export.c.tmdb_id)
        .where(m.Show.popularity.is_distinct_from(export.c.popularity))
        .values(popularity=export.c.popularity)
    )


async def refresh_popularity(
    session: AsyncSession, *, entries: Sequence[ExportEntry], batch_size: int = _BATCH_SIZE
) -> PopularityResult:
    """Write each exported series' popularity onto its mirrored row.

    Caller owns the transaction.

    A series in the export with no row here is skipped silently — the ingest's
    work list is where a missing series is answered, not this pass. A series in
    the mirror and absent from the export keeps whatever popularity it has: that
    is the tombstone pass's question, and answering it here by nulling the
    column would destroy a score over a series TMDB merely stopped listing.

    A line whose popularity did not parse contributes its id to the export
    elsewhere but no score here, so its row is left alone rather than nulled.

    `batch_size` exists so a test can prove the batching rather than trust it;
    production has no reason to pass it.
    """
    live = (
        await session.execute(
            select(func.count())
            .select_from(m.Show)
            # The same population the tombstone pass counts: locally-authored
            # rows were never in the export, and an already-tombstoned row is
            # absent from it by construction, so neither is evidence about how
            # complete the file is.
            .where(m.Show.tmdb_id.is_not(None), m.Show.deleted_upstream_at.is_(None))
        )
    ).scalar_one()

    if reason := feed_is_implausible(len(entries), live):
        log.error("catalog popularity refresh skipped, wrote nothing: %s", reason)
        return PopularityResult(0, 0, reason)

    scored = [e for e in entries if e.popularity is not None]
    updated = 0
    for start in range(0, len(scored), batch_size):
        result = await session.execute(_update_batch(scored[start : start + batch_size]))
        updated += result.rowcount  # type: ignore[attr-defined]

    log.info(
        "catalog popularity refresh: %d of %d exported scores moved a row",
        updated,
        len(scored),
    )
    return PopularityResult(updated, len(scored))
