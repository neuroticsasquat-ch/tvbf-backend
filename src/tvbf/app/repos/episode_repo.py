from datetime import date
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.models import UserEpisodeWatch, UserShowWatch
from tvbf.catalog.episodes import EPISODE_ORDER
from tvbf.catalog.models import Episode


async def get_by_id(db: AsyncSession, episode_id: int) -> Episode | None:
    return await db.get(Episode, episode_id)


async def list_episode_ids_for_season(
    db: AsyncSession, show_id: int, season_number: int
) -> list[int]:
    result = await db.execute(
        select(Episode.id).where(
            Episode.show_id == show_id,
            Episode.season_number == season_number,
        )
    )
    return list(result.scalars().all())


async def aired_count_per_season(db: AsyncSession, show_id: int, today: date) -> dict[int, int]:
    rows = (
        await db.execute(
            select(Episode.season_number, func.count(Episode.id))
            .where(
                Episode.show_id == show_id,
                Episode.air_date.is_not(None),
                Episode.air_date <= today,
            )
            .group_by(Episode.season_number)
        )
    ).all()
    return {season: count for season, count in rows}


async def list_aired_episode_ids_for_show(db: AsyncSession, show_id: int, today: date) -> list[int]:
    result = await db.execute(
        select(Episode.id).where(
            Episode.show_id == show_id,
            Episode.air_date.is_not(None),
            Episode.air_date <= today,
        )
    )
    return list(result.scalars().all())


async def list_episode_ids_for_show(db: AsyncSession, show_id: int) -> list[int]:
    result = await db.execute(select(Episode.id).where(Episode.show_id == show_id))
    return list(result.scalars().all())


async def count_per_show(db: AsyncSession, show_ids: list[int]) -> dict[int, int]:
    """Return total episode count per show_id."""
    rows = (
        await db.execute(
            select(Episode.show_id, func.count(Episode.id))
            .where(Episode.show_id.in_(show_ids))
            .group_by(Episode.show_id)
        )
    ).all()
    return {sid: c for sid, c in rows}


async def count_aired_per_show(
    db: AsyncSession, show_ids: list[int], today: date
) -> dict[int, int]:
    """Return aired episode count per show_id (airdate not null and <= today)."""
    rows = (
        await db.execute(
            select(Episode.show_id, func.count(Episode.id))
            .where(
                Episode.show_id.in_(show_ids),
                Episode.air_date.is_not(None),
                Episode.air_date <= today,
            )
            .group_by(Episode.show_id)
        )
    ).all()
    return {sid: c for sid, c in rows}


async def latest_aired_per_show(
    db: AsyncSession, show_ids: list[int], today: date
) -> dict[int, date]:
    """Return the date of the latest aired episode per show_id."""
    rows = (
        await db.execute(
            select(Episode.show_id, func.max(Episode.air_date))
            .where(
                Episode.show_id.in_(show_ids),
                Episode.air_date.is_not(None),
                Episode.air_date <= today,
            )
            .group_by(Episode.show_id)
        )
    ).all()
    return {sid: t for sid, t in rows}


async def earliest_aired_unwatched_per_show(
    db: AsyncSession, *, user_id: UUID, today: date
) -> list[Episode]:
    """Return the earliest unwatched-and-aired episode per show in the user's
    My Shows list. Used by Watch Next."""
    watched_subq = (
        select(UserEpisodeWatch.episode_id).where(UserEpisodeWatch.user_id == user_id)
    ).subquery()

    base = (
        select(Episode.id)
        .join(UserShowWatch, UserShowWatch.show_id == Episode.show_id)
        .where(
            UserShowWatch.user_id == user_id,
            Episode.air_date.is_not(None),
            Episode.air_date <= today,
            Episode.id.notin_(select(watched_subq)),
        )
    )
    order = EPISODE_ORDER
    rn = func.row_number().over(partition_by=Episode.show_id, order_by=order).label("rn")
    base_with_rn = base.add_columns(rn).subquery()

    ep_ids = (
        (await db.execute(select(base_with_rn.c.id).where(base_with_rn.c.rn == 1))).scalars().all()
    )
    if not ep_ids:
        return []

    rows = (await db.execute(select(Episode).where(Episode.id.in_(ep_ids)))).scalars().all()
    return list(rows)


async def earliest_future_per_show(
    db: AsyncSession, *, user_id: UUID, today: date
) -> list[Episode]:
    """Return the earliest future episode per show in the user's My Shows list.
    Used by Upcoming."""
    base = (
        select(Episode.id)
        .join(UserShowWatch, UserShowWatch.show_id == Episode.show_id)
        .where(
            UserShowWatch.user_id == user_id,
            Episode.air_date.is_not(None),
            Episode.air_date > today,
        )
    )
    order = (Episode.air_date.asc(), *EPISODE_ORDER)
    rn = func.row_number().over(partition_by=Episode.show_id, order_by=order).label("rn")
    base_with_rn = base.add_columns(rn).subquery()

    ep_ids = (
        (await db.execute(select(base_with_rn.c.id).where(base_with_rn.c.rn == 1))).scalars().all()
    )
    if not ep_ids:
        return []

    rows = (await db.execute(select(Episode).where(Episode.id.in_(ep_ids)))).scalars().all()
    return list(rows)


async def next_unwatched(db: AsyncSession, *, user_id: UUID, show_id: int) -> Episode | None:
    """Earliest unwatched episode by season/number ordering, regardless of
    airdate. Used internally by My Shows entries."""
    watched_subq = (
        select(UserEpisodeWatch.episode_id).where(UserEpisodeWatch.user_id == user_id)
    ).subquery()
    stmt = (
        select(Episode)
        .where(Episode.show_id == show_id)
        .where(Episode.id.notin_(select(watched_subq)))
        .order_by(*EPISODE_ORDER)
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()
