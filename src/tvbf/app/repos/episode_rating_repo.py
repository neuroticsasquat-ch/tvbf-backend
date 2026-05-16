from decimal import Decimal
from uuid import UUID

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.models import UserEpisodeRating


async def upsert(
    session: AsyncSession, *, user_id: UUID, episode_id: int, stars: Decimal
) -> UserEpisodeRating:
    stmt = (
        insert(UserEpisodeRating)
        .values(user_id=user_id, episode_id=episode_id, stars=stars)
        .on_conflict_do_update(
            constraint="uq_user_episode_rating",
            set_={"stars": stars, "rated_at": func.now()},
        )
        .returning(UserEpisodeRating)
    )
    result = await session.execute(stmt)
    return result.scalar_one()


async def delete(session: AsyncSession, *, user_id: UUID, episode_id: int) -> int:
    result = await session.execute(
        sa_delete(UserEpisodeRating).where(
            UserEpisodeRating.user_id == user_id,
            UserEpisodeRating.episode_id == episode_id,
        )
    )
    return result.rowcount  # type: ignore[attr-defined]


async def get(session: AsyncSession, *, user_id: UUID, episode_id: int) -> UserEpisodeRating | None:
    return (
        await session.execute(
            select(UserEpisodeRating).where(
                UserEpisodeRating.user_id == user_id,
                UserEpisodeRating.episode_id == episode_id,
            )
        )
    ).scalar_one_or_none()


async def list_for_episode(
    session: AsyncSession, *, episode_id: int, restrict_to: set[UUID]
) -> list[UserEpisodeRating]:
    if not restrict_to:
        return []
    return list(
        (
            await session.execute(
                select(UserEpisodeRating)
                .where(
                    UserEpisodeRating.episode_id == episode_id,
                    UserEpisodeRating.user_id.in_(restrict_to),
                )
                .order_by(UserEpisodeRating.rated_at.desc())
            )
        )
        .scalars()
        .all()
    )


async def get_many_for_user(
    session: AsyncSession, *, user_id: UUID, episode_ids: list[int]
) -> dict[int, float]:
    if not episode_ids:
        return {}
    rows = (
        await session.execute(
            select(UserEpisodeRating.episode_id, UserEpisodeRating.stars).where(
                UserEpisodeRating.user_id == user_id,
                UserEpisodeRating.episode_id.in_(episode_ids),
            )
        )
    ).all()
    return {r.episode_id: float(r.stars) for r in rows}
