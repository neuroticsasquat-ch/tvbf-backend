import math

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.models import User
from tvbf.app.repos import show_membership_repo
from tvbf.catalog import browse_queries
from tvbf.catalog.schemas import (
    ALLOWED_SORT_KEYS,
    AnticipatedShowOut,
    CastMemberOut,
    CrewMemberOut,
    EpisodeOut,
    GenreOut,
    NetworkOut,
    PersonCreditsOut,
    PersonListPage,
    PersonOut,
    SeasonOut,
    ShowDetail,
    ShowFilters,
    ShowListPage,
    ShowSummary,
    TrendingOut,
    TrendingShowOut,
    build_cast_member,
    build_crew_member,
    build_episode_out,
    build_network_out,
    build_network_ref,
    build_person_credits,
    build_person_out,
    build_season_out,
    build_show_detail,
    build_show_summary,
)
from tvbf.deps import get_current_user, get_session


def _set_browse_cache(response: Response) -> None:
    """Browse responses are user-gated (cookie session). Use `private` so
    shared caches (CDN, corporate proxies) aren't authorized to fan out the
    response across users; the requesting browser still caches for max-age."""
    response.headers["Cache-Control"] = "private, max-age=300"


# Browse is gated behind the session cookie — invite-only beta means even the
# catalog isn't public.
router = APIRouter(
    tags=["browse"],
    dependencies=[Depends(get_current_user), Depends(_set_browse_cache)],
)


@router.get("/genres", response_model=list[GenreOut])
async def list_genres(session: AsyncSession = Depends(get_session)) -> list:
    return await browse_queries.list_genres(session)


@router.get("/networks", response_model=list[NetworkOut])
async def list_networks(session: AsyncSession = Depends(get_session)) -> list[NetworkOut]:
    # One list where there were two: TMDB draws no broadcaster/streamer
    # distinction, so `catalog.network` absorbed `tvmaze.web_channel` (audit §6).
    return [build_network_out(n) for n in await browse_queries.list_networks(session)]


# These payloads carry per-user fields (my_rating, ...) that mutate via PUT
# /me/... routes with no way to invalidate the browser HTTP cache. Any max-age
# here causes React Query refetches after a rating change to read stale bodies
# from the browser cache and revert the optimistic UI update.
_SHOW_EP_CACHE = "private, no-store"


@router.get("/shows", response_model=ShowListPage)
async def list_shows_route(
    response: Response,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
    search: str | None = None,
    status: str | None = None,
    genre: list[str] = Query(default_factory=list),
    network: list[int] = Query(default_factory=list),
    language: str | None = None,
    type: str | None = None,
    sort: str = "name",
    page: int = Query(default=1, ge=1, le=1000),
    per_page: int = Query(default=50, ge=1, le=100),
) -> ShowListPage:
    response.headers["Cache-Control"] = _SHOW_EP_CACHE
    if sort not in ALLOWED_SORT_KEYS:
        raise HTTPException(status_code=422, detail=f"invalid sort key: {sort}")

    filters = ShowFilters(
        search=search,
        status=status,
        genres=genre,
        network_ids=network,
        language=language,
        type=type,
    )
    rows, total = await browse_queries.list_shows(
        session, filters, sort=sort, page=page, per_page=per_page
    )
    genres_by_show, networks_by_show = await browse_queries.hydrate_show_refs(session, rows)
    matched_aka_by_show = await browse_queries.hydrate_matched_aka(session, rows, search)
    my_ratings = await browse_queries.hydrate_my_ratings(
        session, viewer_id=user.id, show_ids=[s.id for s in rows]
    )

    items: list[ShowSummary] = [
        build_show_summary(
            show,
            genre_names=genres_by_show.get(show.id, []),
            network=build_network_ref(networks_by_show.get(show.id)),
            matched_aka=matched_aka_by_show.get(show.id),
            my_rating=my_ratings.get(show.id),
        )
        for show in rows
    ]

    return ShowListPage(
        items=items,
        page=page,
        per_page=per_page,
        total=total,
        total_pages=max(1, math.ceil(total / per_page)),
    )


@router.get("/shows/{show_id}", response_model=ShowDetail)
async def get_show(
    show_id: int,
    response: Response,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> ShowDetail:
    response.headers["Cache-Control"] = _SHOW_EP_CACHE
    result = await browse_queries.get_show_with_seasons(session, show_id)
    if result is None:
        raise HTTPException(status_code=404, detail="show not found")
    show, seasons, genres, network = result
    my_ratings = await browse_queries.hydrate_my_ratings(
        session, viewer_id=user.id, show_ids=[show.id]
    )
    return build_show_detail(show, seasons, genres, network, my_rating=my_ratings.get(show.id))


@router.get("/shows/{show_id}/seasons", response_model=list[SeasonOut])
async def get_show_seasons_route(
    show_id: int,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> list[SeasonOut]:
    response.headers["Cache-Control"] = _SHOW_EP_CACHE
    if not await browse_queries.show_exists(session, show_id):
        raise HTTPException(status_code=404, detail="show not found")
    return [build_season_out(s) for s in await browse_queries.get_show_seasons(session, show_id)]


@router.get("/shows/{show_id}/similar", response_model=list[ShowSummary])
async def get_show_similar_route(
    show_id: int,
    session: AsyncSession = Depends(get_session),
) -> list:
    """The twelve shows TMDB recommends alongside this one (NEU-1053).

    `ShowSummary`, so the SPA reuses `ShowCard` / `ShowGrid` unchanged. A show
    with no recommendations answers `200 []` rather than a 404 — that is roughly
    8% of the long tail and the section simply does not render — while an unknown
    show still 404s, on `/cast`'s reasoning: an empty result cannot stand in for a
    missing show once empty is ordinary.

    **The payload carries no per-user field**, which is what lets this route keep
    the router-level `private, max-age=300` instead of the `no-store` the show
    and episode routes need. That is a trade rather than a free win, and it is
    `my_rating` that pays: `ShowCard` *does* render a badge for it, so a
    recommended show the viewer has already rated shows one here where it would
    on any other grid. Filling it costs a query and, by `_SHOW_EP_CACHE`'s rule,
    the cacheability of a list that is identical for every viewer — so the badge
    goes.

    Genres and the network are left empty on the cheaper reasoning: `ShowCard`
    renders neither, so hydrating them is two more round trips for fields nothing
    displays, against an acceptance criterion of one query for the list. A
    consumer wanting the list-view shape (`ShowList` reads both) would need them
    hydrated here.
    """
    if not await browse_queries.show_exists(session, show_id):
        raise HTTPException(status_code=404, detail="show not found")
    return [
        build_show_summary(show, genre_names=[], network=None)
        for show in await browse_queries.list_similar_shows(session, show_id)
    ]


@router.get("/trending", response_model=TrendingOut)
async def get_trending_route(
    response: Response,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> TrendingOut:
    """The current `/trending/tv/week` snapshot, or nothing if it has gone stale.

    **The seven-day cutoff is the server's rule and is enforced in one place**
    (`browse_queries.get_trending_snapshot`, project spec §3). A snapshot past it
    answers `200 {"captured_at": null, "shows": []}` — the same body an empty
    table gives — so the SPA renders no section without ever being told the word
    "stale", and never holds enough to re-derive the rule for itself. A rule
    enforced in two places drifts, and what it drifts into is week-old rows under
    a label reading "trending right now".

    **Shows the viewer already tracks are marked, not filtered.** Trending is a
    claim about the world; seeing your own show in it is a feature.

    That mark is a per-user field, so the route takes `no-store` rather than the
    router-level cacheable header — the reason the show and episode routes do.
    Being per-user anyway is also what pays for `my_rating` here where
    `/shows/{id}/similar` declines it: that route trades the badge for a body
    identical to every viewer's, and this one has no such body to protect.

    Genres and the network are left empty on `/shows/{id}/similar`'s cheaper
    reasoning: `ShowCard` renders neither, so hydrating them would be two more
    round trips for fields nothing displays.
    """
    response.headers["Cache-Control"] = _SHOW_EP_CACHE
    captured_at, shows = await browse_queries.get_trending_snapshot(session)
    show_ids = [show.id for show in shows]
    tracked = await show_membership_repo.tracked_show_ids(
        session, user_id=user.id, show_ids=show_ids
    )
    my_ratings = await browse_queries.hydrate_my_ratings(
        session, viewer_id=user.id, show_ids=show_ids
    )
    return TrendingOut(
        captured_at=captured_at,
        shows=[
            TrendingShowOut(
                **build_show_summary(
                    show,
                    genre_names=[],
                    network=None,
                    my_rating=my_ratings.get(show.id),
                ).model_dump(),
                in_my_shows=show.id in tracked,
            )
            for show in shows
        ],
    )


@router.get("/anticipated", response_model=list[AnticipatedShowOut])
async def get_anticipated_route(
    response: Response,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> list[AnticipatedShowOut]:
    """The shows premiering soonest that most people are waiting for (NEU-1059).

    A thin router over `browse_queries.list_anticipated_shows`, which is a live
    query over `catalog.show` rather than a snapshot table (project spec §4).
    Three things this route would otherwise have to decide are therefore absent
    rather than solved: there is no rule for dropping a show that premiered
    since the list was built, because `current_date` is evaluated on the read;
    no rule for what a failed run leaves behind, because there is no run; and
    **no staleness cutoff of the kind `/trending` carries**, because nothing is
    stored to go stale — and because "anticipated" makes no present-tense claim
    that a week-old answer would falsify.

    A bare array rather than an object, unlike `/trending`: there is no
    `captured_at` to report, so there would be nothing in the wrapper. Nothing
    matching is `200 []` — never a 404, on `/shows/{id}/similar`'s reasoning
    that an empty list is an ordinary answer here, and never a 204, on
    `/me/recommendations`' that the SPA tells "nothing to show" from "the
    request failed" by status code.

    **Shows the viewer already tracks are marked, not filtered**, exactly as on
    `/trending`: a list of what is coming is a claim about the world, and seeing
    something you are already waiting for in it is a feature.

    That mark is a per-user field, so this route takes `no-store` rather than
    the router-level cacheable header. The ticket asked for
    `public, max-age=300` alongside the mark, and neither half of that survives
    it. `public` is the lesser problem — a shared cache authorized to fan the
    body out serves one account's marks to another — and the router-level
    `private` already fixes it. **It is the `max-age` that cannot stay**, for
    `_SHOW_EP_CACHE`'s own reason: the mark is not just per-user but mutable by
    the user, so any max-age lets a My Shows toggle be followed by a refetch
    that reads the pre-toggle body out of the browser cache and reverts the
    optimistic update. `/trending` carries the identical mark and resolved it
    the identical way.
    """
    response.headers["Cache-Control"] = _SHOW_EP_CACHE
    shows = await browse_queries.list_anticipated_shows(session)
    tracked = await show_membership_repo.tracked_show_ids(
        session, user_id=user.id, show_ids=[show.id for show in shows]
    )
    return [
        AnticipatedShowOut(
            **build_show_summary(show, genre_names=[], network=None).model_dump(),
            in_my_shows=show.id in tracked,
        )
        for show in shows
    ]


# Credits are deliberately not embedded in GET /shows/{id}: cast is unbounded
# (The Simpsons has 1,420 cast and 533 crew rows), and the detail route serves a
# card. Separate routes keep that payload bounded and let the SPA lazy-load.
# Both take the router-level `private, max-age=300` — no per-user fields here,
# so no no-store override.
@router.get("/shows/{show_id}/cast", response_model=list[CastMemberOut])
async def get_show_cast_route(
    show_id: int,
    session: AsyncSession = Depends(get_session),
) -> list:
    # A show with no cast is 27% of the catalog, not an error — so an empty
    # result can't stand in for a missing show. Check existence explicitly.
    if not await browse_queries.show_exists(session, show_id):
        raise HTTPException(status_code=404, detail="show not found")
    return [
        build_cast_member(person, character)
        for person, character in await browse_queries.list_show_cast(session, show_id)
    ]


@router.get("/shows/{show_id}/crew", response_model=list[CrewMemberOut])
async def get_show_crew_route(
    show_id: int,
    session: AsyncSession = Depends(get_session),
) -> list:
    if not await browse_queries.show_exists(session, show_id):
        raise HTTPException(status_code=404, detail="show not found")
    return [
        build_crew_member(person, role)
        for person, role in await browse_queries.list_show_crew(session, show_id)
    ]


# Person routes carry no per-user fields, so they take the router-level
# `private, max-age=300` rather than the `no-store` the show/episode routes need.
@router.get("/people", response_model=PersonListPage)
async def search_people_route(
    session: AsyncSession = Depends(get_session),
    # Required: this is a search endpoint, not a browse-all-people one. 487k
    # people paginated alphabetically is not a feature anyone asked for, and
    # serving it would sort the whole table per request. A blank or
    # punctuation-only term is accepted and matches nothing.
    search: str = Query(),
    page: int = Query(default=1, ge=1, le=1000),
    per_page: int = Query(default=50, ge=1, le=100),
) -> PersonListPage:
    rows, total = await browse_queries.search_people(session, search, page=page, per_page=per_page)
    return PersonListPage(
        items=[build_person_out(person) for person in rows],
        page=page,
        per_page=per_page,
        total=total,
        total_pages=max(1, math.ceil(total / per_page)),
    )


@router.get("/people/{person_id}", response_model=PersonOut)
async def get_person_route(
    person_id: int,
    session: AsyncSession = Depends(get_session),
) -> PersonOut:
    person = await browse_queries.get_person(session, person_id)
    if person is None:
        raise HTTPException(status_code=404, detail="person not found")
    return build_person_out(person)


# Filmography is split off from the detail route for the same reason show cast
# is: it's unbounded (Zachary Levi has 61 guest credits alone) while the detail
# payload renders a header card.
@router.get("/people/{person_id}/credits", response_model=PersonCreditsOut)
async def get_person_credits_route(
    person_id: int,
    session: AsyncSession = Depends(get_session),
) -> PersonCreditsOut:
    # Plenty of people in the mirror have no credits at all, so an empty result
    # can't stand in for a missing person. Check existence explicitly.
    if not await browse_queries.person_exists(session, person_id):
        raise HTTPException(status_code=404, detail="person not found")
    # Keyword arguments: the four row lists are same-shaped sequences of tuples,
    # so a transposed pair would fail deep inside Pydantic rather than here.
    return build_person_credits(
        cast_rows=await browse_queries.list_person_cast_credits(session, person_id),
        crew_rows=await browse_queries.list_person_crew_credits(session, person_id),
        guest_rows=await browse_queries.list_person_guest_credits(session, person_id),
        episode_crew_rows=await browse_queries.list_person_episode_crew_credits(session, person_id),
    )


@router.get("/episodes/{episode_id}", response_model=EpisodeOut)
async def get_episode_route(
    episode_id: int,
    response: Response,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> EpisodeOut:
    response.headers["Cache-Control"] = _SHOW_EP_CACHE
    ep = await browse_queries.get_episode(session, episode_id)
    if ep is None:
        raise HTTPException(status_code=404, detail="episode not found")
    my_ratings = await browse_queries.hydrate_my_episode_ratings(
        session, viewer_id=user.id, episode_ids=[ep.id]
    )
    return build_episode_out(ep, my_rating=my_ratings.get(ep.id))


# Guest cast is the episode-level counterpart of /shows/{id}/cast and shares its
# shape, so it reuses CastMemberOut. It carries no per-user fields, so it takes
# the router-level `private, max-age=300` rather than the `no-store` the episode
# detail route needs for `my_rating`.
@router.get("/episodes/{episode_id}/guest-cast", response_model=list[CastMemberOut])
async def get_episode_guest_cast_route(
    episode_id: int,
    session: AsyncSession = Depends(get_session),
) -> list:
    # 96% of episodes have zero guest cast, so an empty result can't stand in
    # for a missing episode. Check existence explicitly.
    if not await browse_queries.episode_exists(session, episode_id):
        raise HTTPException(status_code=404, detail="episode not found")
    return [
        build_cast_member(person, character)
        for person, character in await browse_queries.list_episode_guest_cast(session, episode_id)
    ]


# Episode crew is the episode-level counterpart of /shows/{id}/crew and shares
# its shape, so it reuses CrewMemberOut. Like guest cast it carries no per-user
# fields, so the router-level `private, max-age=300` applies.
@router.get("/episodes/{episode_id}/crew", response_model=list[CrewMemberOut])
async def get_episode_crew_route(
    episode_id: int,
    session: AsyncSession = Depends(get_session),
) -> list:
    # 22.5% of sampled episodes have no crew credits at all (ADR-0003), so an
    # empty result can't stand in for a missing episode. Check existence.
    if not await browse_queries.episode_exists(session, episode_id):
        raise HTTPException(status_code=404, detail="episode not found")
    return [
        build_crew_member(person, role)
        for person, role in await browse_queries.list_episode_crew(session, episode_id)
    ]


@router.get("/shows/{show_id}/episodes", response_model=list[EpisodeOut])
async def get_show_episodes_route(
    show_id: int,
    response: Response,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
    season: int | None = None,
) -> list[EpisodeOut]:
    response.headers["Cache-Control"] = _SHOW_EP_CACHE
    if not await browse_queries.show_exists(session, show_id):
        raise HTTPException(status_code=404, detail="show not found")
    eps = await browse_queries.get_show_episodes(session, show_id, season)
    my_ratings = await browse_queries.hydrate_my_episode_ratings(
        session, viewer_id=user.id, episode_ids=[e.id for e in eps]
    )
    return [build_episode_out(ep, my_rating=my_ratings.get(ep.id)) for ep in eps]
