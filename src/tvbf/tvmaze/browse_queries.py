import unicodedata
from uuid import UUID

from sqlalchemy import false, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.repos import episode_rating_repo, show_rating_repo
from tvbf.sorting import SQL_LEADING_ARTICLE_PATTERN
from tvbf.sql_fold import folded
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


async def episode_exists(session: AsyncSession, episode_id: int) -> bool:
    result = await session.execute(select(m.Episode.id).where(m.Episode.id == episode_id))
    return result.scalar_one_or_none() is not None


async def get_show_episodes(
    session: AsyncSession, show_id: int, season: int | None
) -> list[m.Episode]:
    stmt = select(m.Episode).where(m.Episode.show_id == show_id)
    if season is not None:
        stmt = stmt.where(m.Episode.season == season)
    stmt = stmt.order_by(m.Episode.season, m.Episode.number)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def list_show_cast(
    session: AsyncSession, show_id: int
) -> list[tuple[m.ShowCast, m.Person, m.Character]]:
    """Cast credits for one show in upstream billing order.

    Covered by ix_show_cast_show_id_sort. Single-show route, so unlike
    `hydrate_show_refs` there is no N+1 to batch away.
    """
    stmt = (
        select(m.ShowCast, m.Person, m.Character)
        .join(m.Person, m.Person.id == m.ShowCast.person_id)
        .join(m.Character, m.Character.id == m.ShowCast.character_id)
        .where(m.ShowCast.show_id == show_id)
        .order_by(m.ShowCast.sort_order)
    )
    result = await session.execute(stmt)
    return list(result.tuples().all())


async def list_show_crew(session: AsyncSession, show_id: int) -> list[tuple[m.Person, m.CrewRole]]:
    """Crew credits for one show in upstream order. Covered by ix_show_crew_show_id_sort."""
    stmt = (
        select(m.Person, m.CrewRole)
        .join(m.ShowCrew, m.ShowCrew.person_id == m.Person.id)
        .join(m.CrewRole, m.CrewRole.id == m.ShowCrew.role_id)
        .where(m.ShowCrew.show_id == show_id)
        .order_by(m.ShowCrew.sort_order)
    )
    result = await session.execute(stmt)
    return list(result.tuples().all())


async def list_episode_guest_cast(
    session: AsyncSession, episode_id: int
) -> list[tuple[m.EpisodeGuestCast, m.Person, m.Character]]:
    """Guest-cast credits for one episode in upstream credit order — the
    episode's own credit sequence, not billing order (which is reserved for show
    cast, where upstream orders by total appearances).

    Covered by ix_egc_episode_id_sort. The credit id breaks ties: nothing
    upstream guarantees `sort_order` is distinct within an episode, so the order
    would otherwise be nondeterministic across requests.
    """
    stmt = (
        select(m.EpisodeGuestCast, m.Person, m.Character)
        .join(m.Person, m.Person.id == m.EpisodeGuestCast.person_id)
        .join(m.Character, m.Character.id == m.EpisodeGuestCast.character_id)
        .where(m.EpisodeGuestCast.episode_id == episode_id)
        .order_by(m.EpisodeGuestCast.sort_order.asc(), m.EpisodeGuestCast.id.asc())
    )
    return list((await session.execute(stmt)).tuples().all())


async def list_episode_crew(
    session: AsyncSession, episode_id: int
) -> list[tuple[m.Person, m.EpisodeCrewRole]]:
    """Crew credits for one episode in upstream credit order.

    Covered by ix_episode_crew_episode_id_sort. Same tie-break reasoning as
    `list_episode_guest_cast`, and it bites harder here: one person holds more
    than one crew role on 36 of 1,043 sampled episodes (ADR-0003).
    """
    stmt = (
        select(m.Person, m.EpisodeCrewRole)
        .join(m.EpisodeCrew, m.EpisodeCrew.person_id == m.Person.id)
        .join(m.EpisodeCrewRole, m.EpisodeCrewRole.id == m.EpisodeCrew.role_id)
        .where(m.EpisodeCrew.episode_id == episode_id)
        .order_by(m.EpisodeCrew.sort_order.asc(), m.EpisodeCrew.id.asc())
    )
    return list((await session.execute(stmt)).tuples().all())


async def get_person(session: AsyncSession, person_id: int) -> m.Person | None:
    result = await session.execute(select(m.Person).where(m.Person.id == person_id))
    return result.scalar_one_or_none()


async def person_exists(session: AsyncSession, person_id: int) -> bool:
    result = await session.execute(select(m.Person.id).where(m.Person.id == person_id))
    return result.scalar_one_or_none() is not None


# A filmography reads most-recent-first, so credited shows are ordered by
# premiere date descending. Shows with no premiere date (unaired, upcoming) sort
# last; show id breaks ties so the order is stable across requests.
_CREDIT_SHOW_ORDER = (m.Show.premiered.desc().nulls_last(), m.Show.id.asc())

# Episode-level credits read the same way, by air date. Episodes with no airdate
# (unaired, or never dated upstream) sort last; episode id breaks ties.
_CREDIT_EPISODE_ORDER = (m.Episode.airdate.desc().nulls_last(), m.Episode.id.asc())


async def list_person_cast_credits(
    session: AsyncSession, person_id: int
) -> list[tuple[m.ShowCast, m.Show, m.Character]]:
    """Regular cast credits for one person. Covered by ix_show_cast_person_id."""
    stmt = (
        select(m.ShowCast, m.Show, m.Character)
        .join(m.Show, m.Show.id == m.ShowCast.show_id)
        .join(m.Character, m.Character.id == m.ShowCast.character_id)
        .where(m.ShowCast.person_id == person_id)
        # The credit tables carry no unique constraint by design, so one person
        # can hold several credits on the same show (two characters, or an
        # upstream duplicate). Break the tie on the credit itself, or the order
        # within a show is nondeterministic across requests.
        .order_by(*_CREDIT_SHOW_ORDER, m.ShowCast.sort_order.asc(), m.ShowCast.id.asc())
    )
    return list((await session.execute(stmt)).tuples().all())


async def list_person_crew_credits(
    session: AsyncSession, person_id: int
) -> list[tuple[m.Show, m.CrewRole]]:
    """Crew credits for one person. Covered by ix_show_crew_person_id."""
    stmt = (
        select(m.Show, m.CrewRole)
        .join(m.ShowCrew, m.ShowCrew.show_id == m.Show.id)
        .join(m.CrewRole, m.CrewRole.id == m.ShowCrew.role_id)
        .where(m.ShowCrew.person_id == person_id)
        # Crew is the common multi-credit case — one person is routinely writer
        # and director on the same show — so role name orders within a show, and
        # the credit id keeps even identical roles stable.
        .order_by(*_CREDIT_SHOW_ORDER, m.CrewRole.name.asc(), m.ShowCrew.id.asc())
    )
    return list((await session.execute(stmt)).tuples().all())


async def list_person_guest_credits(
    session: AsyncSession, person_id: int
) -> list[tuple[m.EpisodeGuestCast, m.Episode, m.Show, m.Character]]:
    """Guest-cast credits for one person, joined through episode → show so each
    entry can render "Show — S2E11" without a second round trip.

    Ordered by air date descending (`_CREDIT_EPISODE_ORDER`): `sort_order` on a
    guest credit is credit order within its own episode and says nothing useful
    across episodes. Within one episode the credit id keeps the order stable.
    """
    stmt = (
        select(m.EpisodeGuestCast, m.Episode, m.Show, m.Character)
        .join(m.Episode, m.Episode.id == m.EpisodeGuestCast.episode_id)
        .join(m.Show, m.Show.id == m.Episode.show_id)
        .join(m.Character, m.Character.id == m.EpisodeGuestCast.character_id)
        .where(m.EpisodeGuestCast.person_id == person_id)
        .order_by(*_CREDIT_EPISODE_ORDER, m.EpisodeGuestCast.id.asc())
    )
    return list((await session.execute(stmt)).tuples().all())


async def list_person_episode_crew_credits(
    session: AsyncSession, person_id: int
) -> list[tuple[m.Episode, m.Show, m.EpisodeCrewRole]]:
    """Episode-crew credits for one person, joined through episode → show so each
    entry can render "Show — S1E3" without a second round trip.

    This is the half of a person's filmography that has no upstream person-side
    route at all — `/people/{id}?embed[]=guestcrewcredits` is a 400 — so it only
    exists because credits are fetched per season (ADR-0003).

    Ordered by air date descending like `list_person_guest_credits`, nulls last.
    A person routinely holds both Writer and Director on one episode, and within
    that episode `sort_order` is the credit sequence — the same order
    `list_episode_crew` serves, so the two views of one episode agree.
    """
    stmt = (
        select(m.Episode, m.Show, m.EpisodeCrewRole)
        .join(m.EpisodeCrew, m.EpisodeCrew.episode_id == m.Episode.id)
        .join(m.Show, m.Show.id == m.Episode.show_id)
        .join(m.EpisodeCrewRole, m.EpisodeCrewRole.id == m.EpisodeCrew.role_id)
        .where(m.EpisodeCrew.person_id == person_id)
        .order_by(
            *_CREDIT_EPISODE_ORDER,
            m.EpisodeCrew.sort_order.asc(),
            m.EpisodeCrew.id.asc(),
        )
    )
    return list((await session.execute(stmt)).tuples().all())


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

    # Tombstoned shows are gone upstream and must not be discoverable — nobody
    # should be able to newly find or add one (ADR-0005). Deliberately scoped to
    # discovery: `get_show_with_seasons` and every /me surface still serve them,
    # so a user already tracking one keeps their list, ratings and history.
    base = select(m.Show).where(m.Show.deleted_upstream_at.is_(None))
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
            needle = func.concat("%", folded(literal(token, literal_execute=True)), "%")
            aka_subq = select(m.ShowAka.show_id).where(folded(m.ShowAka.name).like(needle))
            base = base.where(or_(folded(m.Show.name).like(needle), m.Show.id.in_(aka_subq)))
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


async def search_people(
    session: AsyncSession,
    search: str | None,
    page: int,
    per_page: int,
) -> tuple[list[m.Person], int]:
    """Paginated person search — the same query shape as show search, pointed at
    a different table.

    Deliberately a separate entity search rather than a third OR branch in
    `list_shows`: a cast member's name is not a name of the show, and folding
    ~1.3M crew names into the title predicate would make "smith" return most of
    the catalog. `list_shows` is untouched by this.

    Reuses `folded` so the column and each query token normalize under identical
    rules, which matters more for names than for titles — "visnjic" has to reach
    "Goran Višnjić" because nobody types the diacritics. Backed by
    `ix_person_name_folded_trgm`.

    Search-only by design: with no usable token there is nothing to match, so
    this returns an empty page rather than the whole table. There is no
    browse-all-people surface, and at ~487k rows an unfiltered listing would
    sort the entire table on every request off the back of an index that only
    covers the folded name.
    """
    # Token-AND, same as show search: "zachary levi" matches, "zachary garcia"
    # doesn't. A query that folds to nothing ("--", "") matches nothing — never
    # everything.
    usable = [t for t in (search or "").split() if _strip_punct_space(t)]
    if not usable:
        return [], 0

    base = select(m.Person)
    for token in usable:
        needle = func.concat("%", folded(literal(token, literal_execute=True)), "%")
        base = base.where(folded(m.Person.name).like(needle))

    total = (await session.execute(select(func.count()).select_from(base.subquery()))).scalar_one()

    stmt = (
        base.order_by(func.lower(m.Person.name).asc(), m.Person.id.asc())
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
        needle = func.concat("%", folded(literal(token, literal_execute=True)), "%")
        aka_query = aka_query.where(folded(m.ShowAka.name).like(needle))
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
        needle = func.concat("%", folded(literal(token, literal_execute=True)), "%")
        name_query = name_query.where(folded(m.Show.name).like(needle))
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
