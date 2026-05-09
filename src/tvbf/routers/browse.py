import math

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.deps import get_current_user, get_session
from tvbf.tvmaze import browse_queries
from tvbf.tvmaze.schemas import (
    ALLOWED_SORT_KEYS,
    EpisodeOut,
    GenreOut,
    NetworkOut,
    NetworkRef,
    SeasonOut,
    ShowDetail,
    ShowFilters,
    ShowListPage,
    ShowSummary,
    build_show_detail,
    build_show_summary,
)

# Browse is gated behind the session cookie — invite-only beta means even the
# catalog isn't public.
router = APIRouter(tags=["browse"], dependencies=[Depends(get_current_user)])


@router.get("/genres", response_model=list[GenreOut])
async def list_genres(session: AsyncSession = Depends(get_session)) -> list:
    return await browse_queries.list_genres(session)


@router.get("/networks", response_model=list[NetworkOut])
async def list_networks(session: AsyncSession = Depends(get_session)) -> list:
    return await browse_queries.list_networks(session)


@router.get("/shows", response_model=ShowListPage)
async def list_shows_route(
    session: AsyncSession = Depends(get_session),
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
    genres_by_show, networks_by_id, wcs_by_id = await browse_queries.hydrate_show_refs(
        session, rows
    )
    matched_aka_by_show = await browse_queries.hydrate_matched_aka(session, rows, search)

    items: list[ShowSummary] = []
    for show in rows:
        net = networks_by_id.get(show.network_id) if show.network_id is not None else None
        wc = wcs_by_id.get(show.web_channel_id) if show.web_channel_id is not None else None
        items.append(
            build_show_summary(
                show,
                genre_names=genres_by_show.get(show.id, []),
                network=NetworkRef(id=net.id, name=net.name) if net else None,
                web_channel=NetworkRef(id=wc.id, name=wc.name) if wc else None,
                matched_aka=matched_aka_by_show.get(show.id),
            )
        )

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
    session: AsyncSession = Depends(get_session),
) -> ShowDetail:
    result = await browse_queries.get_show_with_seasons(session, show_id)
    if result is None:
        raise HTTPException(status_code=404, detail="show not found")
    show, seasons, genres, network, web_channel = result
    return build_show_detail(show, seasons, genres, network, web_channel)


@router.get("/shows/{show_id}/seasons", response_model=list[SeasonOut])
async def get_show_seasons_route(
    show_id: int,
    session: AsyncSession = Depends(get_session),
) -> list:
    if not await browse_queries.show_exists(session, show_id):
        raise HTTPException(status_code=404, detail="show not found")
    return await browse_queries.get_show_seasons(session, show_id)


@router.get("/episodes/{episode_id}", response_model=EpisodeOut)
async def get_episode_route(
    episode_id: int,
    session: AsyncSession = Depends(get_session),
) -> EpisodeOut:
    ep = await browse_queries.get_episode(session, episode_id)
    if ep is None:
        raise HTTPException(status_code=404, detail="episode not found")
    return EpisodeOut.model_validate(ep)


@router.get("/shows/{show_id}/episodes", response_model=list[EpisodeOut])
async def get_show_episodes_route(
    show_id: int,
    session: AsyncSession = Depends(get_session),
    season: int | None = None,
) -> list:
    if not await browse_queries.show_exists(session, show_id):
        raise HTTPException(status_code=404, detail="show not found")
    return await browse_queries.get_show_episodes(session, show_id, season)
