from collections.abc import AsyncIterator
from datetime import date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.errors import InvalidCredentials, NotFound
from tvbf.app.models import User
from tvbf.app.schemas import (
    AccountDeleteRequest,
    AuthedUserOut,
    BulkSeasonResult,
    EpisodeRatingIn,
    EpisodeRatingOut,
    EpisodeWatchOut,
    MeUpdateRequest,
    MyShowEntry,
    MyShowsSort,
    SeasonProgress,
    SessionSummary,
    ShowRatingIn,
    ShowRatingOut,
    UpcomingEntry,
    UpcomingSeasonEntry,
    UpcomingShowEntry,
    UpcomingSort,
    WatchedEntry,
    WatchedSort,
    WatchedStatusFilter,
    WatchNextEntry,
    WatchNextSort,
)
from tvbf.app.services import (
    account_service,
    episode_service,
    export_service,
    my_shows_service,
    rating_service,
    session_service,
)
from tvbf.config import Settings, get_settings
from tvbf.cookies import clear_auth_cookies
from tvbf.db import SessionLocal
from tvbf.deps import get_current_user, get_session, require_csrf

router = APIRouter(tags=["me"])


@router.get("/me", response_model=AuthedUserOut)
async def me(
    request: Request,
    user: User = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> AuthedUserOut:
    csrf = request.cookies.get(settings.csrf_cookie_name, "")
    return AuthedUserOut(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        created_at=user.created_at,
        email_verified_at=user.email_verified_at,
        csrf_token=csrf,
    )


@router.patch(
    "/me",
    response_model=AuthedUserOut,
    dependencies=[Depends(require_csrf)],
)
async def update_me(
    payload: MeUpdateRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> AuthedUserOut:
    user.display_name = payload.display_name
    await db.commit()
    csrf = request.cookies.get(settings.csrf_cookie_name, "")
    return AuthedUserOut(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        created_at=user.created_at,
        email_verified_at=user.email_verified_at,
        csrf_token=csrf,
    )


@router.get("/me/sessions", response_model=list[SessionSummary])
async def list_my_sessions(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> list[SessionSummary]:
    current_session_id = request.cookies.get(settings.session_cookie_name)
    return await session_service.list_for_user(
        db, user_id=user.id, current_session_id=current_session_id
    )


@router.delete(
    "/me/sessions/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_csrf)],
)
async def revoke_session(
    session_id: Annotated[str, Path()],
    request: Request,
    response: Response,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Response:
    try:
        await session_service.revoke(db, user_id=user.id, session_id=session_id)
    except NotFound as err:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found") from err
    # If the caller just revoked the session they're using, drop their cookies
    # so subsequent requests aren't surprised by an invalidated session.
    current = request.cookies.get(settings.session_cookie_name)
    if current == session_id:
        clear_auth_cookies(response, settings)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.post(
    "/me/sessions/revoke-others",
    dependencies=[Depends(require_csrf)],
)
async def revoke_other_sessions(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, int]:
    current = request.cookies.get(settings.session_cookie_name)
    if not current:
        # Without a current session id we can't safely scope "others" — but
        # we shouldn't be reachable here, since get_current_user already
        # required a valid session cookie.
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="auth_required")
    revoked = await session_service.revoke_others(db, user_id=user.id, current_session_id=current)
    return {"revoked": revoked}


@router.get("/me/export")
async def export_my_data(
    user: User = Depends(get_current_user),
) -> StreamingResponse:
    """Stream the requesting user's account-level data as a JSON document.

    We open a dedicated AsyncSession here (rather than depending on
    `get_session`) because the `get_session` dependency closes its session as
    soon as the route function returns — before the StreamingResponse body
    starts being consumed. The session needs to outlive every `yield` in the
    generator, so we own its lifecycle inline.
    """

    async def _body() -> AsyncIterator[str]:
        async with SessionLocal() as db:
            async for chunk in export_service.stream_export(db, user=user):
                yield chunk

    filename = f"tvbf-export-{datetime.now().date().isoformat()}.json"
    return StreamingResponse(
        _body(),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.delete(
    "/me",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_csrf)],
)
async def delete_me(
    payload: AccountDeleteRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    try:
        await account_service.delete_account(db, user=user, password=payload.password)
    except InvalidCredentials as err:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_credentials"
        ) from err
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# My Shows membership
# ---------------------------------------------------------------------------


@router.get("/me/shows", response_model=list[MyShowEntry])
async def list_my_shows_route(
    sort: Annotated[MyShowsSort, Query()] = "recent_activity",
    today: Annotated[date | None, Query()] = None,
    rated_only: Annotated[bool, Query()] = False,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> list[MyShowEntry]:
    return await my_shows_service.list_my_shows(
        db, user_id=user.id, sort=sort, today=today, rated_only=rated_only
    )


@router.put(
    "/me/shows/{show_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_csrf)],
)
async def add_show_route(
    show_id: Annotated[int, Path()],
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    try:
        await my_shows_service.add(db, user_id=user.id, show_id=show_id)
    except NotFound as err:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found") from err
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/me/shows/{show_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_csrf)],
)
async def remove_show_route(
    show_id: Annotated[int, Path()],
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    await my_shows_service.remove(db, user_id=user.id, show_id=show_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Watch Next + Upcoming
# ---------------------------------------------------------------------------


@router.get("/me/watch-next", response_model=list[WatchNextEntry])
async def watch_next_route(
    sort: Annotated[WatchNextSort, Query()] = "airdate_desc",
    today: Annotated[date | None, Query()] = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> list[WatchNextEntry]:
    return await my_shows_service.list_watch_next(db, user_id=user.id, sort=sort, today=today)


@router.get("/me/upcoming", response_model=list[UpcomingEntry])
async def upcoming_route(
    sort: Annotated[UpcomingSort, Query()] = "airdate_asc",
    today: Annotated[date | None, Query()] = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> list[UpcomingEntry]:
    return await my_shows_service.list_upcoming(db, user_id=user.id, sort=sort, today=today)


@router.get("/me/upcoming/seasons", response_model=list[UpcomingSeasonEntry])
async def upcoming_seasons_route(
    sort: Annotated[UpcomingSort, Query()] = "airdate_asc",
    today: Annotated[date | None, Query()] = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> list[UpcomingSeasonEntry]:
    return await my_shows_service.list_upcoming_seasons(db, user_id=user.id, sort=sort, today=today)


@router.get("/me/upcoming/shows", response_model=list[UpcomingShowEntry])
async def upcoming_shows_route(
    sort: Annotated[UpcomingSort, Query()] = "airdate_asc",
    today: Annotated[date | None, Query()] = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> list[UpcomingShowEntry]:
    return await my_shows_service.list_upcoming_shows(db, user_id=user.id, sort=sort, today=today)


@router.get("/me/watched", response_model=list[WatchedEntry])
async def watched_route(
    status: Annotated[WatchedStatusFilter, Query()] = "all",
    sort: Annotated[WatchedSort, Query()] = "last_watched_desc",
    today: Annotated[date | None, Query()] = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> list[WatchedEntry]:
    return await my_shows_service.list_watched(
        db, user_id=user.id, status=status, sort=sort, today=today
    )


# ---------------------------------------------------------------------------
# Per-episode watch state
# ---------------------------------------------------------------------------


@router.get("/me/shows/{show_id}/episodes/watched", response_model=list[int])
async def list_watched_episodes_for_show(
    show_id: Annotated[int, Path()],
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> list[int]:
    return await episode_service.list_watched_episode_ids(db, user_id=user.id, show_id=show_id)


@router.post(
    "/me/episodes/{episode_id}/watched",
    status_code=status.HTTP_201_CREATED,
    response_model=EpisodeWatchOut,
    dependencies=[Depends(require_csrf)],
)
async def mark_episode_watched(
    episode_id: Annotated[int, Path()],
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> EpisodeWatchOut:
    try:
        return await episode_service.mark_episode(db, user_id=user.id, episode_id=episode_id)
    except NotFound as err:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found") from err


@router.delete(
    "/me/episodes/{episode_id}/watched",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_csrf)],
)
async def unmark_episode_watched(
    episode_id: Annotated[int, Path()],
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    await episode_service.unmark_episode(db, user_id=user.id, episode_id=episode_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Bulk season mark/unmark
# ---------------------------------------------------------------------------


@router.post(
    "/me/shows/{show_id}/season/{season_number}/watched",
    status_code=status.HTTP_201_CREATED,
    response_model=BulkSeasonResult,
    dependencies=[Depends(require_csrf)],
)
async def bulk_mark_season(
    show_id: Annotated[int, Path()],
    season_number: Annotated[int, Path()],
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> BulkSeasonResult:
    try:
        count = await episode_service.bulk_mark_season(
            db, user_id=user.id, show_id=show_id, season_number=season_number
        )
    except NotFound as err:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found") from err
    return BulkSeasonResult(marked=count)


@router.delete(
    "/me/shows/{show_id}/season/{season_number}/watched",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_csrf)],
)
async def bulk_unmark_season(
    show_id: Annotated[int, Path()],
    season_number: Annotated[int, Path()],
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    await episode_service.bulk_unmark_season(
        db, user_id=user.id, show_id=show_id, season_number=season_number
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/me/shows/{show_id}/seasons/progress",
    response_model=list[SeasonProgress],
)
async def list_season_progress(
    show_id: Annotated[int, Path()],
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> list[SeasonProgress]:
    rows = await episode_service.list_season_progress(db, user_id=user.id, show_id=show_id)
    return [SeasonProgress(**r) for r in rows]


# ---------------------------------------------------------------------------
# Bulk show mark/unmark
# ---------------------------------------------------------------------------


@router.post(
    "/me/shows/{show_id}/watched",
    status_code=status.HTTP_201_CREATED,
    response_model=BulkSeasonResult,
    dependencies=[Depends(require_csrf)],
)
async def bulk_mark_show(
    show_id: Annotated[int, Path()],
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> BulkSeasonResult:
    try:
        count = await episode_service.bulk_mark_show(db, user_id=user.id, show_id=show_id)
    except NotFound as err:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found") from err
    return BulkSeasonResult(marked=count)


@router.delete(
    "/me/shows/{show_id}/watched",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_csrf)],
)
async def bulk_unmark_show(
    show_id: Annotated[int, Path()],
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    await episode_service.bulk_unmark_show(db, user_id=user.id, show_id=show_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Ratings (show + episode)
# ---------------------------------------------------------------------------


@router.put(
    "/me/shows/{show_id}/rating",
    response_model=ShowRatingOut,
    dependencies=[Depends(require_csrf)],
)
async def set_show_rating(
    payload: ShowRatingIn,
    show_id: Annotated[int, Path()],
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> ShowRatingOut:
    try:
        out = await rating_service.set_show_rating(
            db, user_id=user.id, show_id=show_id, stars=payload.stars
        )
    except NotFound as err:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="show_not_found") from err
    await db.commit()
    return out


@router.delete(
    "/me/shows/{show_id}/rating",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_csrf)],
)
async def clear_show_rating(
    show_id: Annotated[int, Path()],
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    await rating_service.clear_show_rating(db, user_id=user.id, show_id=show_id)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put(
    "/me/episodes/{episode_id}/rating",
    response_model=EpisodeRatingOut,
    dependencies=[Depends(require_csrf)],
)
async def set_episode_rating(
    payload: EpisodeRatingIn,
    episode_id: Annotated[int, Path()],
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> EpisodeRatingOut:
    try:
        out = await rating_service.set_episode_rating(
            db, user_id=user.id, episode_id=episode_id, stars=payload.stars
        )
    except NotFound as err:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="episode_not_found"
        ) from err
    await db.commit()
    return out


@router.delete(
    "/me/episodes/{episode_id}/rating",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_csrf)],
)
async def clear_episode_rating(
    episode_id: Annotated[int, Path()],
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    await rating_service.clear_episode_rating(db, user_id=user.id, episode_id=episode_id)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
