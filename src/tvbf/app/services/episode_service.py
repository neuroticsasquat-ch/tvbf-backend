from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.dto import EpisodeWatchOut
from tvbf.app.errors import NotFound
from tvbf.app.repos import episode_repo, episode_watch_repo


async def mark_episode(db: AsyncSession, *, user_id: UUID, episode_id: int) -> EpisodeWatchOut:
    """Verify episode exists, upsert watch row, commit. Raises NotFound if
    episode does not exist."""
    ep = await episode_repo.get_by_id(db, episode_id)
    if ep is None:
        raise NotFound()

    now = datetime.now(UTC)
    actual_watched_at = await episode_watch_repo.mark(
        db, user_id=user_id, episode_id=episode_id, watched_at=now
    )
    await db.commit()
    return EpisodeWatchOut(episode_id=episode_id, watched_at=actual_watched_at)


async def unmark_episode(db: AsyncSession, *, user_id: UUID, episode_id: int) -> None:
    await episode_watch_repo.unmark(db, user_id=user_id, episode_id=episode_id)
    await db.commit()


async def list_watched_episode_ids(db: AsyncSession, *, user_id: UUID, show_id: int) -> list[int]:
    return await episode_watch_repo.list_episode_ids_for_show(db, user_id=user_id, show_id=show_id)


async def bulk_mark_season(
    db: AsyncSession, *, user_id: UUID, show_id: int, season_number: int
) -> int:
    """Fetch episode ids for the season; raise NotFound if none exist; bulk-insert
    watch rows; commit; return count of episodes marked."""
    ep_ids = await episode_repo.list_episode_ids_for_season(db, show_id, season_number)
    if not ep_ids:
        raise NotFound()

    now = datetime.now(UTC)
    await episode_watch_repo.bulk_mark(db, user_id=user_id, episode_ids=ep_ids, watched_at=now)
    await db.commit()
    return len(ep_ids)


async def bulk_unmark_season(
    db: AsyncSession, *, user_id: UUID, show_id: int, season_number: int
) -> None:
    ep_ids = await episode_repo.list_episode_ids_for_season(db, show_id, season_number)
    if ep_ids:
        await episode_watch_repo.bulk_unmark(db, user_id=user_id, episode_ids=ep_ids)
        await db.commit()
