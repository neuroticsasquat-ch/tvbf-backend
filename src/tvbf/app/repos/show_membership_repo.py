from datetime import datetime
from uuid import UUID

from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.models import UserShowWatch
from tvbf.tvmaze.models import Show


async def add(db: AsyncSession, *, user_id: UUID, show_id: int) -> None:
    """Idempotent INSERT … ON CONFLICT DO NOTHING."""
    stmt = pg_insert(UserShowWatch).values(user_id=user_id, show_id=show_id)
    stmt = stmt.on_conflict_do_nothing(index_elements=["user_id", "show_id"])
    await db.execute(stmt)


async def remove(db: AsyncSession, *, user_id: UUID, show_id: int) -> None:
    await db.execute(
        sa_delete(UserShowWatch).where(
            UserShowWatch.user_id == user_id,
            UserShowWatch.show_id == show_id,
        )
    )


async def user_ids_with_show(
    db: AsyncSession, *, show_id: int, restrict_to: set[UUID]
) -> set[UUID]:
    """Return the subset of `restrict_to` user_ids that have this show in
    their My Shows. Returns empty set if `restrict_to` is empty."""
    if not restrict_to:
        return set()
    rows = (
        (
            await db.execute(
                select(UserShowWatch.user_id).where(
                    UserShowWatch.show_id == show_id,
                    UserShowWatch.user_id.in_(restrict_to),
                )
            )
        )
        .scalars()
        .all()
    )
    return set(rows)


async def list_with_added_at(db: AsyncSession, user_id: UUID) -> list[tuple[Show, datetime]]:
    """Return (Show, added_at) pairs for all shows the user is tracking."""
    rows = (
        await db.execute(
            select(Show, UserShowWatch.created_at)
            .join(UserShowWatch, UserShowWatch.show_id == Show.id)
            .where(UserShowWatch.user_id == user_id)
        )
    ).all()
    return [(show, added_at) for show, added_at in rows]
