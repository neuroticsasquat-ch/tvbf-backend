"""Writing and reading catalog events — `catalog.show_event` (project spec §4.1).

The delta's change detection (§5.1) is the only writer and the push delivery
job (§5.2) the only reader. Neither decides anything here: detection works out
*which* transitions happened, delivery works out *who* hears about them, and
this module is the table's two access paths and nothing else.
"""

from collections.abc import Collection, Sequence
from datetime import datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.catalog import models as m

# The vocabulary `ck_show_event_kind` enforces, as a type so a caller's typo
# fails pyright rather than the constraint.
ShowEventKind = Literal["premiere_set", "premiere_moved", "ended", "revived"]


async def record_events(session: AsyncSession, events: Sequence[m.ShowEvent]) -> None:
    """Add `events` to the caller's transaction and flush them.

    Flushed, not committed: the delta records a show's events in the same
    transaction as the upsert that revealed them, so the two land or roll back
    together.
    """
    if not events:
        return
    session.add_all(events)
    await session.flush()


async def recent_events(
    session: AsyncSession, *, since: datetime, kinds: Collection[ShowEventKind]
) -> list[m.ShowEvent]:
    """Events of `kinds` observed at or after `since`, oldest first.

    Oldest first because that is the order delivery spends a user's daily cap
    in (§5.2 step 3); `id` breaks ties, since one show's events share their
    transaction's `now()`.
    """
    if not kinds:
        return []
    result = await session.execute(
        select(m.ShowEvent)
        .where(m.ShowEvent.observed_at >= since, m.ShowEvent.kind.in_(kinds))
        .order_by(m.ShowEvent.observed_at, m.ShowEvent.id)
    )
    return list(result.scalars())
