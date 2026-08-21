"""The handle-release ledger (NEU-1163 §4.2).

Every handle any account has given up, and the record that decides whether a
handle nobody currently holds may be claimed. It is also the change throttle's
counter — NEU-1162's shape, where the row is written anyway so there is no side
table and no "record the attempt" step to forget.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.models import HandleRelease


async def get(db: AsyncSession, handle: str) -> HandleRelease | None:
    result = await db.execute(select(HandleRelease).where(HandleRelease.handle == handle))
    return result.scalar_one_or_none()


async def record(db: AsyncSession, *, handle: str, user_id: UUID) -> None:
    """Note that `user_id` has given `handle` up. Caller commits.

    `ON CONFLICT DO UPDATE` rather than a plain insert, because the same account
    may release the same handle more than once — claim it, drop it, reclaim it,
    drop it again. The last release is the one the throttle should count, and
    the owner is unchanged by construction: a row is only ever rewritten by the
    account that could reclaim it in the first place.
    """
    stmt = pg_insert(HandleRelease).values(handle=handle, user_id=user_id)
    await db.execute(
        stmt.on_conflict_do_update(
            index_elements=[HandleRelease.handle],
            set_={"user_id": user_id, "released_at": func.now()},
        )
    )


async def count_since(db: AsyncSession, *, user_id: UUID, since: datetime) -> int:
    """How many handles this account has released inside the window (§6.2).

    **This counts distinct handles, not distinct changes**, because `handle` is
    the primary key and the claim ledger is what this table is primarily for.
    The consequence is worth stating rather than discovering: oscillating
    between two handles you already own refreshes two rows forever and never
    reaches a cap of three. Every other pattern — three new handles in a month,
    or a fourth change to anything not already released — is caught. Closing
    the oscillation case needs a second, append-only ledger, which §6.2
    deliberately declined to add.
    """
    return (
        await db.execute(
            select(func.count())
            .select_from(HandleRelease)
            .where(HandleRelease.user_id == user_id, HandleRelease.released_at > since)
        )
    ).scalar_one()
