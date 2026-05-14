from decimal import Decimal
from uuid import UUID

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.models import UserShowRating


async def upsert(
    session: AsyncSession, *, user_id: UUID, show_id: int, stars: Decimal
) -> UserShowRating:
    stmt = (
        insert(UserShowRating)
        .values(user_id=user_id, show_id=show_id, stars=stars)
        .on_conflict_do_update(
            constraint="uq_user_show_rating",
            set_={"stars": stars, "rated_at": func.now()},
        )
        .returning(UserShowRating)
    )
    result = await session.execute(stmt)
    return result.scalar_one()


async def delete(session: AsyncSession, *, user_id: UUID, show_id: int) -> int:
    result = await session.execute(
        sa_delete(UserShowRating).where(
            UserShowRating.user_id == user_id,
            UserShowRating.show_id == show_id,
        )
    )
    return result.rowcount  # type: ignore[attr-defined]


async def get(session: AsyncSession, *, user_id: UUID, show_id: int) -> UserShowRating | None:
    return (
        await session.execute(
            select(UserShowRating).where(
                UserShowRating.user_id == user_id,
                UserShowRating.show_id == show_id,
            )
        )
    ).scalar_one_or_none()


async def list_for_show(
    session: AsyncSession, *, show_id: int, restrict_to: set[UUID]
) -> list[UserShowRating]:
    if not restrict_to:
        return []
    return list(
        (
            await session.execute(
                select(UserShowRating)
                .where(
                    UserShowRating.show_id == show_id,
                    UserShowRating.user_id.in_(restrict_to),
                )
                .order_by(UserShowRating.rated_at.desc())
            )
        )
        .scalars()
        .all()
    )


async def get_many_for_user(
    session: AsyncSession, *, user_id: UUID, show_ids: list[int]
) -> dict[int, float]:
    if not show_ids:
        return {}
    rows = (
        await session.execute(
            select(UserShowRating.show_id, UserShowRating.stars).where(
                UserShowRating.user_id == user_id,
                UserShowRating.show_id.in_(show_ids),
            )
        )
    ).all()
    return {r.show_id: float(r.stars) for r in rows}
