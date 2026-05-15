from decimal import Decimal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.errors import NotFound
from tvbf.app.repos import (
    episode_rating_repo,
    episode_repo,
    show_rating_repo,
    show_repo,
    user_repo,
)
from tvbf.app.schemas import (
    EpisodeRatingOut,
    FriendRatingItem,
    FriendRatingsResponse,
    ShowRatingOut,
)
from tvbf.app.services import activity_service, connection_service


async def set_show_rating(
    db: AsyncSession, *, user_id: UUID, show_id: int, stars: Decimal
) -> ShowRatingOut:
    if await show_repo.get_by_id(db, show_id) is None:
        raise NotFound("show_not_found")
    row = await show_rating_repo.upsert(db, user_id=user_id, show_id=show_id, stars=stars)
    await activity_service.emit(
        db,
        actor_id=user_id,
        verb="rated_show",
        target_type="show",
        target_id=show_id,
        payload={"stars": float(stars)},
    )
    return ShowRatingOut(show_id=show_id, stars=float(row.stars), rated_at=row.rated_at)


async def clear_show_rating(db: AsyncSession, *, user_id: UUID, show_id: int) -> int:
    deleted = await show_rating_repo.delete(db, user_id=user_id, show_id=show_id)
    await activity_service.cancel(
        db, actor_id=user_id, verb="rated_show", target_type="show", target_id=show_id
    )
    return deleted


async def set_episode_rating(
    db: AsyncSession, *, user_id: UUID, episode_id: int, stars: Decimal
) -> EpisodeRatingOut:
    if await episode_repo.get_by_id(db, episode_id) is None:
        raise NotFound("episode_not_found")
    row = await episode_rating_repo.upsert(db, user_id=user_id, episode_id=episode_id, stars=stars)
    await activity_service.emit(
        db,
        actor_id=user_id,
        verb="rated_episode",
        target_type="episode",
        target_id=episode_id,
        payload={"stars": float(stars)},
    )
    return EpisodeRatingOut(episode_id=episode_id, stars=float(row.stars), rated_at=row.rated_at)


async def clear_episode_rating(db: AsyncSession, *, user_id: UUID, episode_id: int) -> int:
    deleted = await episode_rating_repo.delete(db, user_id=user_id, episode_id=episode_id)
    await activity_service.cancel(
        db,
        actor_id=user_id,
        verb="rated_episode",
        target_type="episode",
        target_id=episode_id,
    )
    return deleted


async def friend_show_ratings(
    db: AsyncSession, *, viewer_id: UUID, show_id: int
) -> FriendRatingsResponse:
    if await show_repo.get_by_id(db, show_id) is None:
        raise NotFound("show_not_found")
    friends = await connection_service.accepted_friend_ids(db, viewer_id)
    rows = await show_rating_repo.list_for_show(db, show_id=show_id, restrict_to=friends)
    users = await user_repo.get_many_by_ids(db, {r.user_id for r in rows})
    items = [
        FriendRatingItem(
            user_id=r.user_id,
            display_name=users[r.user_id].display_name,
            stars=float(r.stars),
            rated_at=r.rated_at,
        )
        for r in rows
        if r.user_id in users
    ]
    avg = round(sum(i.stars for i in items) / len(items), 1) if items else None
    return FriendRatingsResponse(avg=avg, count=len(items), items=items)


async def friend_episode_ratings(
    db: AsyncSession, *, viewer_id: UUID, episode_id: int
) -> FriendRatingsResponse:
    if await episode_repo.get_by_id(db, episode_id) is None:
        raise NotFound("episode_not_found")
    friends = await connection_service.accepted_friend_ids(db, viewer_id)
    rows = await episode_rating_repo.list_for_episode(
        db, episode_id=episode_id, restrict_to=friends
    )
    users = await user_repo.get_many_by_ids(db, {r.user_id for r in rows})
    items = [
        FriendRatingItem(
            user_id=r.user_id,
            display_name=users[r.user_id].display_name,
            stars=float(r.stars),
            rated_at=r.rated_at,
        )
        for r in rows
        if r.user_id in users
    ]
    avg = round(sum(i.stars for i in items) / len(items), 1) if items else None
    return FriendRatingsResponse(avg=avg, count=len(items), items=items)
