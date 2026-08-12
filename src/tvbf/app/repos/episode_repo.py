"""Episode reads for the tracking layer.

Every query here names its own specials predicate — `~IS_SPECIAL`,
`~IS_COPIED_SPECIAL`, or nothing at all — rather than inheriting one from a
shared base selectable. `catalog/episodes.py` explains why, and
`tests/integration/app/repos/test_specials_ledger.py` holds every site and its
treatment in one table (NEU-1062).
"""

from datetime import date
from uuid import UUID

from sqlalchemy import func, not_, select
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.models import UserEpisodeWatch, UserShowWatch
from tvbf.catalog.episodes import EPISODE_ORDER, IS_COPIED_SPECIAL, IS_SPECIAL
from tvbf.catalog.models import Episode


async def get_by_id(db: AsyncSession, episode_id: int) -> Episode | None:
    """Any episode, specials included — it serves a special's own episode page."""
    return await db.get(Episode, episode_id)


async def list_episode_ids_for_season(
    db: AsyncSession, show_id: int, season_number: int
) -> list[int]:
    """A season's episodes, minus any copied special hanging inside it.

    Season 0 is *not* excluded: marking the Specials season is a deliberate act
    and stays available. What a regular season must not sweep up is the invented
    negative numbers that belong to no season anyone picked.
    """
    result = await db.execute(
        select(Episode.id).where(
            Episode.show_id == show_id,
            Episode.season_number == season_number,
            not_(IS_COPIED_SPECIAL),
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
                not_(IS_COPIED_SPECIAL),
            )
            .group_by(Episode.season_number)
        )
    ).all()
    return {season: count for season, count in rows}


async def list_aired_episode_ids_for_show(db: AsyncSession, show_id: int, today: date) -> list[int]:
    """Every aired *regular* episode — what "mark the whole show watched" marks.

    Specials count toward nothing, so ticking them would leave the show short of
    100% by exactly the rows the user just asked to be done with.
    """
    result = await db.execute(
        select(Episode.id).where(
            Episode.show_id == show_id,
            Episode.air_date.is_not(None),
            Episode.air_date <= today,
            not_(IS_SPECIAL),
        )
    )
    return list(result.scalars().all())


async def list_episode_ids_for_show(db: AsyncSession, show_id: int) -> list[int]:
    """Every episode, specials included — this backs *un*-marking a whole show.

    Excluding anything here orphans the watch rows for whatever it excluded.
    """
    result = await db.execute(select(Episode.id).where(Episode.show_id == show_id))
    return list(result.scalars().all())


async def count_per_show(db: AsyncSession, show_ids: list[int]) -> dict[int, int]:
    """Return total *regular* episode count per show_id."""
    rows = (
        await db.execute(
            select(Episode.show_id, func.count(Episode.id))
            .where(Episode.show_id.in_(show_ids), not_(IS_SPECIAL))
            .group_by(Episode.show_id)
        )
    ).all()
    return {sid: c for sid, c in rows}


async def count_aired_per_show(
    db: AsyncSession, show_ids: list[int], today: date
) -> dict[int, int]:
    """Return aired *regular* episode count per show_id (airdate <= today).

    The denominator of every progress bar, which is the whole ticket: a user who
    has watched every regular episode and none of the specials reads 100%.
    """
    rows = (
        await db.execute(
            select(Episode.show_id, func.count(Episode.id))
            .where(
                Episode.show_id.in_(show_ids),
                Episode.air_date.is_not(None),
                Episode.air_date <= today,
                not_(IS_SPECIAL),
            )
            .group_by(Episode.show_id)
        )
    ).all()
    return {sid: c for sid, c in rows}


async def latest_aired_per_show(
    db: AsyncSession, show_ids: list[int], today: date
) -> dict[int, date]:
    """Return the date of the latest aired *regular* episode per show_id."""
    rows = (
        await db.execute(
            select(Episode.show_id, func.max(Episode.air_date))
            .where(
                Episode.show_id.in_(show_ids),
                Episode.air_date.is_not(None),
                Episode.air_date <= today,
                not_(IS_SPECIAL),
            )
            .group_by(Episode.show_id)
        )
    ).all()
    return {sid: t for sid, t in rows}


async def earliest_aired_unwatched_per_show(
    db: AsyncSession, *, user_id: UUID, today: date
) -> list[Episode]:
    """Return the earliest unwatched-and-aired *regular* episode per show in the
    user's My Shows list. Used by Watch Next, which must never offer a special as
    the next thing to watch."""
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
            not_(IS_SPECIAL),
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
    """Return the earliest future *regular* episode per show in the user's My
    Shows list. Used by Upcoming: an unaired special is not what a show is
    waiting for."""
    base = (
        select(Episode.id)
        .join(UserShowWatch, UserShowWatch.show_id == Episode.show_id)
        .where(
            UserShowWatch.user_id == user_id,
            Episode.air_date.is_not(None),
            Episode.air_date > today,
            not_(IS_SPECIAL),
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
    """Earliest unwatched *regular* episode by season/number ordering, regardless
    of airdate. Used internally by My Shows entries."""
    watched_subq = (
        select(UserEpisodeWatch.episode_id).where(UserEpisodeWatch.user_id == user_id)
    ).subquery()
    stmt = (
        select(Episode)
        .where(Episode.show_id == show_id)
        .where(Episode.id.notin_(select(watched_subq)))
        .where(not_(IS_SPECIAL))
        .order_by(*EPISODE_ORDER)
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()
