"""Tombstone shows TV Maze has deleted, and resurrect any that come back.

A show absent from `/updates/shows` — the authoritative full list of every show
id upstream holds — is gone. The row is marked, never removed: `app.user_show_watch`
and `app.user_show_rating` cascade from `tvmaze.show`, so a delete would destroy a
user's My Shows entry, rating and watch history, and nothing upstream knows they
existed. See ADR-0005.

Validated against prod 2026-08-06: the feed carried 88,997 ids against 88,971
mirrored, and the mirrored-but-absent set was exactly the 58 shows independently
confirmed 404 by probing `/shows/{id}` — no false positives.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.tvmaze import models as m

log = logging.getLogger(__name__)

# The reverse diff is `mirrored - feed`, so a truncated or empty feed that still
# returns 200 would tombstone the entire catalogue. These floors are the only
# thing standing between a bad upstream response and 89k tombstoned shows —
# NEU-967's empty-embed footgun, one level up.
#
# Absolute: catches an empty or badly truncated 200. Set well below the ~89k
# real size but far above any plausible legitimate collapse.
_MIN_FEED_ABSOLUTE = 50_000
# Relative: catches a partial feed large enough to clear the absolute floor.
# Upstream does not shed 5% of its catalogue in a day.
_MIN_FEED_RELATIVE = 0.95

# Postgres caps bind parameters at 32,767 — the same ceiling that forces
# _EPISODE_BATCH_SIZE. The write sets here are normally tiny, but the first run
# after an upstream purge could be large, so both updates batch their id lists.
_ID_BATCH_SIZE = 1000


@dataclass(frozen=True)
class TombstoneResult:
    tombstoned: int
    resurrected: int
    skipped_reason: str | None = None


def _feed_is_implausible(feed_size: int, mirrored: int) -> str | None:
    """Return why the feed can't be trusted to prove absence, or None if it can."""
    if feed_size < _MIN_FEED_ABSOLUTE:
        return f"feed carried {feed_size} ids, under the absolute floor of {_MIN_FEED_ABSOLUTE}"
    if mirrored and feed_size < _MIN_FEED_RELATIVE * mirrored:
        return (
            f"feed carried {feed_size} ids against {mirrored} mirrored shows, "
            f"under {_MIN_FEED_RELATIVE:.0%} of the mirror"
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
    """Mark shows absent from the feed; unmark any that reappeared.

    Caller owns the transaction.

    When the feed fails a plausibility floor, **nothing is written at all** —
    including resurrections. A feed we don't trust to prove absence is not one
    we should trust to prove presence either.

    The diff is computed in Python rather than as `id NOT IN (:feed)`: the feed
    is ~89k ids and Postgres caps bind parameters at 32,767, so the obvious
    query cannot run. Both write sets are small, and are batched anyway.
    """
    rows = (await session.execute(select(m.Show.id, m.Show.deleted_upstream_at))).all()

    # Compare the feed against LIVE shows only. Already-tombstoned rows are
    # absent from the feed by construction, so counting them would make the
    # relative floor self-wedging: once the tombstoned population passed 5% of
    # the mirror the guard would trip on every run, permanently blocking both
    # tombstoning and resurrection.
    live = sum(1 for r in rows if r.deleted_upstream_at is None)

    if reason := _feed_is_implausible(len(feed_ids), live):
        log.error("tombstone pass skipped, wrote nothing: %s", reason)
        return TombstoneResult(0, 0, reason)

    to_tombstone = [r.id for r in rows if r.deleted_upstream_at is None and r.id not in feed_ids]
    to_resurrect = [r.id for r in rows if r.deleted_upstream_at is not None and r.id in feed_ids]

    await _set_deleted_at(session, show_ids=to_tombstone, value=datetime.now(UTC))
    await _set_deleted_at(session, show_ids=to_resurrect, value=None)

    if to_tombstone:
        log.info(
            "tombstoned %d show(s) absent from /updates/shows: %s",
            len(to_tombstone),
            to_tombstone[:20],
        )
    if to_resurrect:
        log.info(
            "resurrected %d show(s) that reappeared upstream: %s",
            len(to_resurrect),
            to_resurrect[:20],
        )

    return TombstoneResult(len(to_tombstone), len(to_resurrect))
