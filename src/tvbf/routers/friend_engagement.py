"""Friend engagement endpoints surfacing on show + episode pages (NEU-111).

Both endpoints return only the caller's *accepted* connections (pending and
blocked are excluded by `connection_repo.list_accepted_for_user`).
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, status
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.errors import NotFound
from tvbf.app.models import User
from tvbf.app.repos import (
    connection_repo,
    episode_repo,
    episode_watch_repo,
    show_membership_repo,
    show_repo,
    user_repo,
)
from tvbf.app.schemas import FriendRatingsResponse, ShowFriendActivity, UserBrief
from tvbf.app.services import rating_service
from tvbf.deps import get_current_user, get_session

router = APIRouter(tags=["friends"])


async def _accepted_friend_ids(db: AsyncSession, user_id: UUID) -> set[UUID]:
    pairs = await connection_repo.list_accepted_for_user(db, user_id)
    return {other for _, other in pairs}


def _briefs(user_ids: set[UUID], users_by_id: dict[UUID, User]) -> list[UserBrief]:
    """Build sorted UserBrief list, dropping any IDs that didn't hydrate."""
    briefs = [
        UserBrief(id=users_by_id[uid].id, display_name=users_by_id[uid].display_name)
        for uid in user_ids
        if uid in users_by_id
    ]
    briefs.sort(key=lambda b: b.display_name.lower())
    return briefs


@router.get("/shows/{show_id}/friends", response_model=ShowFriendActivity)
async def show_friends(
    show_id: int = Path(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> ShowFriendActivity:
    show = await show_repo.get_by_id(db, show_id)
    if show is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="show_not_found")

    friend_ids = await _accepted_friend_ids(db, user.id)
    in_my = await show_membership_repo.user_ids_with_show(
        db, show_id=show.id, restrict_to=friend_ids
    )
    watched = await episode_watch_repo.user_ids_who_watched_show(
        db, show_id=show.id, restrict_to=friend_ids
    )

    users = await user_repo.get_many_by_ids(db, in_my | watched)
    return ShowFriendActivity(
        in_my_shows=_briefs(in_my, users),
        watched=_briefs(watched, users),
    )


@router.get("/episodes/{episode_id}/friends/watched", response_model=list[UserBrief])
async def episode_friends_watched(
    episode_id: int = Path(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> list[UserBrief]:
    episode = await episode_repo.get_by_id(db, episode_id)
    if episode is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="episode_not_found")

    friend_ids = await _accepted_friend_ids(db, user.id)
    watched_ids = await episode_watch_repo.user_ids_who_watched_episode(
        db, episode_id=episode.id, restrict_to=friend_ids
    )
    users = await user_repo.get_many_by_ids(db, watched_ids)
    return _briefs(watched_ids, users)


@router.get("/shows/{show_id}/friends/ratings", response_model=FriendRatingsResponse)
async def show_friend_ratings(
    show_id: int = Path(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> FriendRatingsResponse:
    try:
        return await rating_service.friend_show_ratings(db, viewer_id=user.id, show_id=show_id)
    except NotFound as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e


@router.get("/episodes/{episode_id}/friends/ratings", response_model=FriendRatingsResponse)
async def episode_friend_ratings(
    episode_id: int = Path(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> FriendRatingsResponse:
    try:
        return await rating_service.friend_episode_ratings(
            db, viewer_id=user.id, episode_id=episode_id
        )
    except NotFound as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
