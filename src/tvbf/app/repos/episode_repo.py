from datetime import date
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.models import UserEpisodeWatch, UserShowWatch
from tvbf.tvmaze.models import Episode


async def get_by_id(db: AsyncSession, episode_id: int) -> Episode | None:
    return await db.get(Episode, episode_id)


async def list_episode_ids_for_season(
    db: AsyncSession, show_id: int, season_number: int
) -> list[int]:
    result = await db.execute(
        select(Episode.id).where(
            Episode.show_id == show_id,
            Episode.season == season_number,
        )
    )
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
                Episode.airdate.is_not(None),
                Episode.airdate <= today,
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
            select(Episode.show_id, func.max(Episode.airdate))
            .where(
                Episode.show_id.in_(show_ids),
                Episode.airdate.is_not(None),
                Episode.airdate <= today,
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
            Episode.airdate.is_not(None),
            Episode.airdate <= today,
            Episode.id.notin_(select(watched_subq)),
        )
    )
    order = (Episode.season.asc(), Episode.number.asc())
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
            Episode.airdate.is_not(None),
            Episode.airdate > today,
        )
    )
    order = (Episode.airdate.asc(), Episode.season.asc(), Episode.number.asc())
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
        .order_by(Episode.season.asc(), Episode.number.asc())
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()
