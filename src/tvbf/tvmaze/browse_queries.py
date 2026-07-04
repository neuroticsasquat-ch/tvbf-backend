import unicodedata
from uuid import UUID

from sqlalchemy import false, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.repos import episode_rating_repo, show_rating_repo
from tvbf.sorting import SQL_LEADING_ARTICLE_PATTERN
from tvbf.tvmaze import models as m
from tvbf.tvmaze.schemas import ALLOWED_SORT_KEYS, ShowFilters

# Strip leading articles for natural alphabetical sort: "The Office" → "office".
_NORMALIZED_NAME = func.regexp_replace(func.lower(m.Show.name), SQL_LEADING_ARTICLE_PATTERN, "")

# Most recent already-aired episode airdate per show. Correlated subquery so it can
# participate in ORDER BY without a join that would multiply rows.
_LAST_AIRED = (
    select(func.max(m.Episode.airdate))
    .where(m.Episode.show_id == m.Show.id)
    .where(m.Episode.airdate <= func.current_date())
    .correlate(m.Show)
    .scalar_subquery()
)

_SORT_EXPRS = {
    "name": _NORMALIZED_NAME.asc(),
    "-name": _NORMALIZED_NAME.desc(),
    "premiered": m.Show.premiered.asc().nulls_last(),
    "-premiered": m.Show.premiered.desc().nulls_last(),
    "tvmaze_updated": m.Show.tvmaze_updated.asc(),
    "-tvmaze_updated": m.Show.tvmaze_updated.desc(),
    "last_aired": _LAST_AIRED.asc().nulls_last(),
    "-last_aired": _LAST_AIRED.desc().nulls_last(),
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


def _fold(expr):
    """Accent- and punctuation-folded form of a text SQL expression or value.

    Strips punctuation and whitespace (preserving letters of every script),
    lowercases, then removes diacritics via the ``unaccent`` extension. Applied
    identically to the searched column and to the query token so both sides of
    the comparison normalize under the same rules — "shogun" matches "Shōgun"
    and "spiderman" matches "Spider-Man", while non-Latin scripts pass through
    unchanged so native-title search keeps working.
    """
    stripped = func.regexp_replace(expr, "[[:punct:][:space:]]+", "", "g")
    return func.immutable_unaccent(func.lower(stripped))


def _strip_punct_space(token: str) -> str:
    """Token with punctuation and whitespace removed. Used only to detect tokens
    that fold to nothing (e.g. "--"): ``unaccent`` never maps a non-empty letter
    to empty, so emptiness depends solely on the punctuation/space strip."""
    return "".join(
        c for c in token if not (unicodedata.category(c)[0] in ("P", "Z") or c.isspace())
    )


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
        # Token-based AND match against an accent- and punctuation-folded form of
        # the show name OR any of its AKAs. Folding both the column and the token
        # lets "shogun" match "Shōgun" and "spiderman" match "Spider-Man", while
        # whitespace tokenization keeps "alien earth" matching "Alien: Earth" and
        # non-Latin titles ("進撃") still match natively.
        usable = [t for t in filters.search.split() if _strip_punct_space(t)]
        if not usable:
            # Search was all punctuation/whitespace — match nothing, not everything.
            base = base.where(false())
        for token in usable:
            needle = func.concat("%", _fold(literal(token, literal_execute=True)), "%")
            aka_subq = select(m.ShowAka.show_id).where(_fold(m.ShowAka.name).like(needle))
            base = base.where(or_(_fold(m.Show.name).like(needle), m.Show.id.in_(aka_subq)))
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


async def hydrate_matched_aka(
    session: AsyncSession, shows: list[m.Show], search: str | None
) -> dict[int, str | None]:
    """Per-show: which AKA (if any) matched the search?

    Returns a dict mapping show_id → matched_aka (or None when the show's own
    name carries the match, or when there's no search term). Empty dict when
    `shows` is empty or `search` is falsy. Used by the browse list route to
    surface match context to the frontend so users see why a foreign-titled
    show came back for an English query.

    Picks the shortest matching AKA per show — heuristic for "most canonical".
    """
    if not search or not shows:
        return {}

    tokens = [t for t in search.split() if _strip_punct_space(t)]
    if not tokens:
        return {}

    show_ids = [s.id for s in shows]

    # Best (shortest) AKA per show that matches every folded token.
    aka_query = select(m.ShowAka.show_id, m.ShowAka.name).where(m.ShowAka.show_id.in_(show_ids))
    for token in tokens:
        needle = func.concat("%", _fold(literal(token, literal_execute=True)), "%")
        aka_query = aka_query.where(_fold(m.ShowAka.name).like(needle))
    aka_rows = (await session.execute(aka_query)).all()
    best_by_show: dict[int, str] = {}
    for sid, aname in aka_rows:
        if sid not in best_by_show or len(aname) < len(best_by_show[sid]):
            best_by_show[sid] = aname

    # Which shows matched on their own (folded) name? Determined in SQL so the
    # rule is identical to list_shows — a Python unaccent would diverge on
    # characters like ł/ø that NFKD does not decompose.
    name_query = select(m.Show.id).where(m.Show.id.in_(show_ids))
    for token in tokens:
        needle = func.concat("%", _fold(literal(token, literal_execute=True)), "%")
        name_query = name_query.where(_fold(m.Show.name).like(needle))
    name_matched_ids = set((await session.execute(name_query)).scalars().all())

    result: dict[int, str | None] = {}
    for show in shows:
        if show.id in name_matched_ids:
            result[show.id] = None
        else:
            result[show.id] = best_by_show.get(show.id)
    return result


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


async def hydrate_my_ratings(
    session: AsyncSession, *, viewer_id: UUID, show_ids: list[int]
) -> dict[int, float]:
    """Per-show: the viewer's own rating (stars) if any. Empty dict when no
    inputs or no viewer ratings. Stars come back as float for JSON-friendliness."""
    return await show_rating_repo.get_many_for_user(session, user_id=viewer_id, show_ids=show_ids)


async def hydrate_my_episode_ratings(
    session: AsyncSession, *, viewer_id: UUID, episode_ids: list[int]
) -> dict[int, float]:
    """Per-episode: the viewer's own rating (stars) if any."""
    return await episode_rating_repo.get_many_for_user(
        session, user_id=viewer_id, episode_ids=episode_ids
    )
