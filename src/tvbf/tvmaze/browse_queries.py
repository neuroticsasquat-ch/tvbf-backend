from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.sorting import SQL_LEADING_ARTICLE_PATTERN
from tvbf.tvmaze import models as m
from tvbf.tvmaze.dto import ALLOWED_SORT_KEYS, ShowFilters

# Strip leading articles for natural alphabetical sort: "The Office" → "office".
_NORMALIZED_NAME = func.regexp_replace(func.lower(m.Show.name), SQL_LEADING_ARTICLE_PATTERN, "")

_SORT_EXPRS = {
    "name": _NORMALIZED_NAME.asc(),
    "-name": _NORMALIZED_NAME.desc(),
    "premiered": m.Show.premiered.asc().nulls_last(),
    "-premiered": m.Show.premiered.desc().nulls_last(),
    "tvmaze_updated": m.Show.tvmaze_updated.asc(),
    "-tvmaze_updated": m.Show.tvmaze_updated.desc(),
}


async def list_genres(session: AsyncSession) -> list[m.Genre]:
    result = await session.execute(select(m.Genre).order_by(m.Genre.name))
    return list(result.scalars().all())


async def list_networks(session: AsyncSession) -> list[m.Network]:
    result = await session.execute(select(m.Network).order_by(m.Network.name))
    return list(result.scalars().all())


async def get_show_with_seasons(
    session: AsyncSession, show_id: int
) -> tuple[m.Show, list[m.Season], list[m.Genre], m.Network | None, m.WebChannel | None] | None:
    show = (await session.execute(select(m.Show).where(m.Show.id == show_id))).scalar_one_or_none()
    if show is None:
        return None

    seasons = list(
        (
            await session.execute(
                select(m.Season).where(m.Season.show_id == show_id).order_by(m.Season.number)
            )
        )
        .scalars()
        .all()
    )

    genres = list(
        (
            await session.execute(
                select(m.Genre)
                .join(m.ShowGenre, m.ShowGenre.genre_id == m.Genre.id)
                .where(m.ShowGenre.show_id == show_id)
                .order_by(m.Genre.name)
            )
        )
        .scalars()
        .all()
    )

    network = None
    if show.network_id is not None:
        network = (
            await session.execute(select(m.Network).where(m.Network.id == show.network_id))
        ).scalar_one_or_none()

    web_channel = None
    if show.web_channel_id is not None:
        web_channel = (
            await session.execute(
                select(m.WebChannel).where(m.WebChannel.id == show.web_channel_id)
            )
        ).scalar_one_or_none()

    return show, seasons, genres, network, web_channel


async def get_show_seasons(session: AsyncSession, show_id: int) -> list[m.Season]:
    result = await session.execute(
        select(m.Season).where(m.Season.show_id == show_id).order_by(m.Season.number)
    )
    return list(result.scalars().all())


async def show_exists(session: AsyncSession, show_id: int) -> bool:
    result = await session.execute(select(m.Show.id).where(m.Show.id == show_id))
    return result.scalar_one_or_none() is not None


async def get_episode(session: AsyncSession, episode_id: int) -> m.Episode | None:
    result = await session.execute(select(m.Episode).where(m.Episode.id == episode_id))
    return result.scalar_one_or_none()


async def get_show_episodes(
    session: AsyncSession, show_id: int, season: int | None
) -> list[m.Episode]:
    stmt = select(m.Episode).where(m.Episode.show_id == show_id)
    if season is not None:
        stmt = stmt.where(m.Episode.season == season)
    stmt = stmt.order_by(m.Episode.season, m.Episode.number)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def list_shows(
    session: AsyncSession,
    filters: ShowFilters,
    sort: str,
    page: int,
    per_page: int,
) -> tuple[list[m.Show], int]:
    if sort not in ALLOWED_SORT_KEYS:
        raise ValueError(f"invalid sort key: {sort}")

    base = select(m.Show)
    if filters.search:
        # Token-based AND match: every whitespace-separated token must appear
        # as a substring (case-insensitive) of the show name. Lets "alien earth"
        # match "Alien: Earth" and "the office us" match "The Office (US)" —
        # punctuation between tokens stops mattering.
        for token in filters.search.split():
            base = base.where(m.Show.name.ilike(f"%{token}%"))
    if filters.status is not None:
        base = base.where(m.Show.status == filters.status)
    if filters.language is not None:
        base = base.where(m.Show.language == filters.language)
    if filters.type is not None:
        base = base.where(m.Show.type == filters.type)
    if filters.genres:
        genre_subq = (
            select(m.ShowGenre.show_id)
            .join(m.Genre, m.Genre.id == m.ShowGenre.genre_id)
            .where(m.Genre.name.in_(filters.genres))
            .group_by(m.ShowGenre.show_id)
            .having(func.count(func.distinct(m.Genre.id)) == len(filters.genres))
        )
        base = base.where(m.Show.id.in_(genre_subq))
    if filters.network_ids:
        base = base.where(m.Show.network_id.in_(filters.network_ids))

    total = (await session.execute(select(func.count()).select_from(base.subquery()))).scalar_one()

    stmt = (
        base.order_by(_SORT_EXPRS[sort], m.Show.id.asc())
        .limit(per_page)
        .offset((page - 1) * per_page)
    )
    rows = list((await session.execute(stmt)).scalars().all())
    return rows, total


async def hydrate_show_refs(
    session: AsyncSession, shows: list[m.Show]
) -> tuple[dict[int, list[str]], dict[int, m.Network], dict[int, m.WebChannel]]:
    if not shows:
        return {}, {}, {}

    show_ids = [s.id for s in shows]
    net_ids = {s.network_id for s in shows if s.network_id is not None}
    wc_ids = {s.web_channel_id for s in shows if s.web_channel_id is not None}

    genre_rows = (
        await session.execute(
            select(m.ShowGenre.show_id, m.Genre.name)
            .join(m.Genre, m.Genre.id == m.ShowGenre.genre_id)
            .where(m.ShowGenre.show_id.in_(show_ids))
        )
    ).all()
    genres_by_show: dict[int, list[str]] = {sid: [] for sid in show_ids}
    for sid, gname in genre_rows:
        genres_by_show[sid].append(gname)

    networks_by_id: dict[int, m.Network] = {}
    if net_ids:
        for row in (
            (await session.execute(select(m.Network).where(m.Network.id.in_(net_ids))))
            .scalars()
            .all()
        ):
            networks_by_id[row.id] = row

    wcs_by_id: dict[int, m.WebChannel] = {}
    if wc_ids:
        for row in (
            (await session.execute(select(m.WebChannel).where(m.WebChannel.id.in_(wc_ids))))
            .scalars()
            .all()
        ):
            wcs_by_id[row.id] = row

    return genres_by_show, networks_by_id, wcs_by_id
