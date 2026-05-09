from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.models import User
from tvbf.app.repos import connection_repo, user_repo
from tvbf.app.schemas import (
    MyShowEntry,
    MyShowsSort,
    UserSearchResult,
    WatchedEntry,
    WatchedSort,
    WatchedStatusFilter,
)
from tvbf.app.services import connection_service, my_shows_service
from tvbf.deps import get_current_user, get_session

router = APIRouter(tags=["users"])

SEARCH_LIMIT = 20
MIN_QUERY_LENGTH = 2


@router.get("/users/search", response_model=list[UserSearchResult])
async def search_users(
    q: Annotated[str, Query()],
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> list[UserSearchResult]:
    if len(q) < MIN_QUERY_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="query_too_short",
        )

    blocked_ids = await connection_repo.list_blocked_user_ids(db, user.id)
    excluded = blocked_ids | {user.id}
    rows = await user_repo.search(db, query=q, limit=SEARCH_LIMIT, exclude_ids=excluded)
    return [UserSearchResult(id=row.id, display_name=row.display_name) for row in rows]


# ---------------------------------------------------------------------------
# Friend library (NEU-108)
# ---------------------------------------------------------------------------

_FRIEND_CACHE_HEADER = "private, max-age=60"


async def _require_connected_friend(db: AsyncSession, *, caller: User, target_id: UUID) -> User:
    """Return the target User if they exist AND the caller is `accepted`-
    connected to them. Otherwise raise 404 — never 403, to avoid leaking
    whether the user exists or whether the connection exists."""
    if target_id == caller.id:
        # Callers should use /me/* for their own data.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")
    target = await user_repo.get_by_id(db, target_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")
    if not await connection_service.are_connected(db, caller.id, target.id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")
    return target


def _set_friend_cache(response: Response) -> None:
    response.headers["Cache-Control"] = _FRIEND_CACHE_HEADER


@router.get("/users/{user_id}/shows", response_model=list[MyShowEntry])
async def friend_shows(
    response: Response,
    user_id: UUID = Path(...),
    sort: Annotated[MyShowsSort, Query()] = "recent_activity",
    today: Annotated[date | None, Query()] = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> list[MyShowEntry]:
    friend = await _require_connected_friend(db, caller=user, target_id=user_id)
    _set_friend_cache(response)
    return await my_shows_service.list_my_shows(db, user_id=friend.id, sort=sort, today=today)


@router.get("/users/{user_id}/watched", response_model=list[WatchedEntry])
async def friend_watched(
    response: Response,
    user_id: UUID = Path(...),
    status_filter: Annotated[WatchedStatusFilter, Query(alias="status")] = "all",
    sort: Annotated[WatchedSort, Query()] = "last_watched_desc",
    today: Annotated[date | None, Query()] = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> list[WatchedEntry]:
    friend = await _require_connected_friend(db, caller=user, target_id=user_id)
    _set_friend_cache(response)
    return await my_shows_service.list_watched(
        db, user_id=friend.id, status=status_filter, sort=sort, today=today
    )
