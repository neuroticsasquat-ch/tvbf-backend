"""Friend-scoped readers: the engagement strips on show + episode pages
(NEU-111) and the Popular with Friends list (NEU-1499).

Every route reads the caller's friends through
`connection_service.accepted_friend_ids`, so pending, blocked and disabled
connections contribute nothing.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.errors import NotFound
from tvbf.app.models import User
from tvbf.app.repos import (
    activity_repo,
    episode_repo,
    episode_watch_repo,
    show_membership_repo,
    show_repo,
    user_repo,
)
from tvbf.app.schemas import FriendRatingsResponse, ShowFriendActivity, UserBrief
from tvbf.app.services import connection_service, rating_service
from tvbf.catalog import browse_queries
from tvbf.catalog.schemas import PopularShowOut, PopularWithFriendsOut, build_show_summary
from tvbf.deps import get_current_user, get_session

router = APIRouter(tags=["friends"])


def _briefs(user_ids: set[UUID], users_by_id: dict[UUID, User]) -> list[UserBrief]:
    """Build sorted UserBrief list, dropping any IDs that didn't hydrate."""
    briefs = [
        UserBrief(
            id=users_by_id[uid].id,
            display_name=users_by_id[uid].display_name,
            handle=users_by_id[uid].handle,
        )
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

    friend_ids = await connection_service.accepted_friend_ids(db, user.id)
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

    friend_ids = await connection_service.accepted_friend_ids(db, user.id)
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


@router.get("/me/friends/popular", response_model=PopularWithFriendsOut)
async def get_popular_with_friends_route(
    response: Response,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> PopularWithFriendsOut:
    """The shows the viewer's friends have been visibly active on lately, ranked
    by how many of them were (project spec §5.2).

    The ranking, window, cap and both sharing switches are
    `activity_repo.popular_shows_for_friends`'s; this route hydrates and never
    re-sorts. Empty is `200` with the true `connection_count`, which is how the
    SPA tells "no friends yet" from "a quiet fortnight".

    `no-store` because `in_my_shows` and `my_rating` are per-user *and* mutable
    through `/me/*`, with no way to invalidate the browser cache — `/trending`'s
    reason. Genres and the network stay empty on its reasoning too: `ShowCard`
    renders neither.
    """
    response.headers["Cache-Control"] = "private, no-store"
    friend_ids = await connection_service.accepted_friend_ids(db, user.id)
    ranked = await activity_repo.popular_shows_for_friends(db, friend_ids=list(friend_ids))
    show_ids = [row.show_id for row in ranked]
    shows = await show_repo.get_many_by_ids(db, show_ids)
    tracked = await show_membership_repo.tracked_show_ids(db, user_id=user.id, show_ids=show_ids)
    my_ratings = await browse_queries.hydrate_my_ratings(db, viewer_id=user.id, show_ids=show_ids)
    return PopularWithFriendsOut(
        window_days=activity_repo.POPULAR_WINDOW_DAYS,
        connection_count=len(friend_ids),
        shows=[
            PopularShowOut(
                **build_show_summary(
                    shows[row.show_id],
                    genre_names=[],
                    network=None,
                    my_rating=my_ratings.get(row.show_id),
                ).model_dump(),
                in_my_shows=row.show_id in tracked,
                friend_count=row.friend_count,
            )
            for row in ranked
            # The ranking already joins `catalog.show`; this only drops a row
            # whose show was deleted between the two statements.
            if row.show_id in shows
        ],
    )
