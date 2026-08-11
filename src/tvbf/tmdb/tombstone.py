"""Tombstone series TMDB has deleted, and resurrect any that come back (NEU-1036).

ADR-0005 against the new spine, amended by ADR-0009. A series absent from the daily id export — the
authoritative full list of every series TMDB holds (see `export.py`) — is gone.
The row is marked, never removed, and every reason that asymmetry exists
survived the source change intact: `app.user_show_watch` and
`app.user_show_rating` cascade from the show row, `app.user_episode_watch`
cascades through episode, `import_ne.show_resolution` references it with NO
ACTION, and `app.activity_event` is polymorphic with no FK at all, so it orphans
silently. A delete destroys user data nothing upstream could restore.

This is a sibling of `tvmaze/tombstone.py` rather than a generalisation of it.
The two diff different keys against differently-shaped feeds under differently
calibrated floors, and the TV Maze one is being retired — a shared abstraction
would couple the survivor to the module that is going away, for forty lines.

## Three things differ from the TV Maze version, and each is load-bearing

**The diff key is `tmdb_id`, not the primary key.** `catalog.show.id` is an
internal surrogate `app` references and the migration seeded from TV Maze's ids
(ADR-0008), so it means nothing to the export. The feed carries TMDB ids; the
comparison has to as well.

**Locally-authored rows are exempt, structurally.** A row with `tmdb_id IS NULL`
was never in the export, so `mirrored - feed` would flag every one of them on
the first pass — including the TV Maze specials NEU-1042 copied in, whose whole
purpose is to hold watch history TMDB cannot supply. They are excluded from the
work list *and* from the plausibility count, because they are not evidence
either way about how complete the feed is.

**The floors are recalibrated against 228,611**, the export's measured size on
2026-08-07, rather than TV Maze's ~89k.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.catalog import models as m

log = logging.getLogger(__name__)

# The export's size the last time it was measured: 2026-08-07. Not a limit, a
# *denominator* — see `_feed_is_implausible` for why the relative floor cannot
# use the mirror's own size the way TV Maze's did.
_MEASURED_EXPORT = 228_611

# The reverse diff is `mirrored - feed`, so a truncated export that still parses
# would tombstone the entire catalog. These floors are the only thing standing
# between a bad download and 228k tombstoned series.
#
# Absolute: catches an empty or badly truncated file with no estimate involved.
# Sized as TV Maze's was — comfortably below the measured catalog (two thirds of
# it), far above any collapse TMDB could legitimately have. Strictly weaker than
# the relative floor under today's constants, and kept anyway: it is what still
# fires if `_MEASURED_EXPORT` is ever revised downward or the mirror count is
# wrong, neither of which it depends on.
_MIN_FEED_ABSOLUTE = 150_000
# Relative: catches a partial file large enough to clear the absolute floor.
# Upstream does not shed 5% of its catalog in a day.
_MIN_FEED_RELATIVE = 0.95

# Postgres caps bind parameters at 32,767 — the same ceiling that forces
# `_EPISODE_BATCH_SIZE` and the export diff being computed in Python. The write
# sets here are normally tiny, but the first run after an upstream purge could
# be large, so both updates batch their id lists.
_ID_BATCH_SIZE = 1000


@dataclass(frozen=True)
class TombstoneResult:
    tombstoned: int
    resurrected: int
    skipped_reason: str | None = None


def _feed_is_implausible(feed_size: int, mirrored: int) -> str | None:
    """Return why the export can't be trusted to prove absence, or None if it can.

    The relative floor measures the export against **the larger of the mirror
    and the export's own measured size**, which is the one place this cannot be
    a straight port. TV Maze's mirror was the same size as its feed, so `95% of
    mirrored` was `95% of the feed` in all but name. Here the mirror is far
    smaller than the export for the whole pre-cutover period — ~63k mapped rows
    against ~229k series — and a fraction of it is no floor at all: a complete-
    looking export carrying a third of the catalog would clear both guards and
    tombstone every mapped series in the missing two thirds.

    Taking the maximum keeps both eras honest. Before cutover the constant binds;
    after it, the mirror overtakes the constant and the guard tracks reality
    rather than a number measured in 2026. Either way an export that has genuinely
    shrunk 5% writes nothing and says so, which is the correct thing to do about
    a catalog that lost eleven thousand series overnight.
    """
    if feed_size < _MIN_FEED_ABSOLUTE:
        return f"export carried {feed_size} ids, under the absolute floor of {_MIN_FEED_ABSOLUTE}"
    expected = max(mirrored, _MEASURED_EXPORT)
    if feed_size < _MIN_FEED_RELATIVE * expected:
        return (
            f"export carried {feed_size} ids against the {expected} series TMDB is known "
            f"to hold, under {_MIN_FEED_RELATIVE:.0%} of it"
        )
    return None


async def _set_deleted_at(
    session: AsyncSession, *, show_ids: list[int], value: datetime | None
) -> None:
    for start in range(0, len(show_ids), _ID_BATCH_SIZE):
        chunk = show_ids[start : start + _ID_BATCH_SIZE]
        await session.execute(
            update(m.Show).where(m.Show.id.in_(chunk)).values(deleted_upstream_at=value)
        )


async def reconcile_tombstones(session: AsyncSession, *, feed_ids: set[int]) -> TombstoneResult:
    """Mark series absent from the export; unmark any that reappeared.

    Caller owns the transaction.

    When the export fails a plausibility floor, **nothing is written at all** —
    including resurrections. A feed we don't trust to prove absence is not one
    we should trust to prove presence either.

    The diff is computed in Python rather than as `tmdb_id NOT IN (:feed)`: the
    export is ~229k ids and Postgres caps bind parameters at 32,767, so the
    obvious query cannot run. Both write sets are small, and are batched anyway.
    """
    rows = (
        await session.execute(
            select(m.Show.id, m.Show.tmdb_id, m.Show.deleted_upstream_at).where(
                # Locally-authored rows were never in the export. Diffing them
                # against it would tombstone every one of them, which is the
                # exact opposite of what they exist for.
                m.Show.tmdb_id.is_not(None)
            )
        )
    ).all()

    # Compare the export against LIVE series only. Already-tombstoned rows are
    # absent from it by construction, so counting them would make the relative
    # floor self-wedging: once the tombstoned population passed 5% of the mirror
    # the guard would trip on every run, permanently blocking both tombstoning
    # and resurrection.
    live = sum(1 for r in rows if r.deleted_upstream_at is None)

    if reason := _feed_is_implausible(len(feed_ids), live):
        log.error("catalog tombstone pass skipped, wrote nothing: %s", reason)
        return TombstoneResult(0, 0, reason)

    to_tombstone = [
        r.id for r in rows if r.deleted_upstream_at is None and r.tmdb_id not in feed_ids
    ]
    to_resurrect = [
        r.id for r in rows if r.deleted_upstream_at is not None and r.tmdb_id in feed_ids
    ]

    await _set_deleted_at(session, show_ids=to_tombstone, value=datetime.now(UTC))
    await _set_deleted_at(session, show_ids=to_resurrect, value=None)

    if to_tombstone:
        log.info(
            "tombstoned %d series absent from the id export: %s",
            len(to_tombstone),
            to_tombstone[:20],
        )
    if to_resurrect:
        log.info(
            "resurrected %d series that reappeared upstream: %s",
            len(to_resurrect),
            to_resurrect[:20],
        )

    return TombstoneResult(len(to_tombstone), len(to_resurrect))
