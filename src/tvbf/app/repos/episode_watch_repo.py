from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, func, select
from sqlalchemy import delete as sa_delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.models import UserEpisodeWatch
from tvbf.tvmaze.models import Episode


async def mark(
    db: AsyncSession,
    *,
    user_id: UUID,
    episode_id: int,
    watched_at: datetime,
) -> datetime:
    """Upsert ON CONFLICT DO NOTHING; returns the actual stored watched_at
    (handles the "already exists" case)."""
    stmt = pg_insert(UserEpisodeWatch).values(
        user_id=user_id, episode_id=episode_id, watched_at=watched_at
    )
    stmt = stmt.on_conflict_do_nothing(index_elements=["user_id", "episode_id"])
    await db.execute(stmt)
    existing = await db.get(
        UserEpisodeWatch,
        (user_id, episode_id),
        populate_existing=True,
    )
    return existing.watched_at if existing else watched_at


async def unmark(db: AsyncSession, *, user_id: UUID, episode_id: int) -> None:
    await db.execute(
        sa_delete(UserEpisodeWatch).where(
            and_(
                UserEpisodeWatch.user_id == user_id,
                UserEpisodeWatch.episode_id == episode_id,
            )
        )
    )


async def user_ids_who_watched_show(
    db: AsyncSession, *, show_id: int, restrict_to: set[UUID]
) -> set[UUID]:
    """Return the subset of `restrict_to` user_ids that have watched at least
    one episode of this show. Empty set if `restrict_to` is empty."""
    if not restrict_to:
        return set()
    rows = (
        (
            await db.execute(
                select(UserEpisodeWatch.user_id)
                .distinct()
                .join(Episode, Episode.id == UserEpisodeWatch.episode_id)
                .where(
                    Episode.show_id == show_id,
                    UserEpisodeWatch.user_id.in_(restrict_to),
                )
            )
        )
        .scalars()
        .all()
    )
    return set(rows)


async def user_ids_who_watched_episode(
    db: AsyncSession, *, episode_id: int, restrict_to: set[UUID]
) -> set[UUID]:
    """Return the subset of `restrict_to` user_ids that watched this episode."""
    if not restrict_to:
        return set()
    rows = (
        (
            await db.execute(
                select(UserEpisodeWatch.user_id).where(
                    UserEpisodeWatch.episode_id == episode_id,
                    UserEpisodeWatch.user_id.in_(restrict_to),
                )
            )
        )
        .scalars()
        .all()
    )
    return set(rows)


async def list_show_ids_with_watches(db: AsyncSession, *, user_id: UUID) -> list[int]:
    """Return distinct show_ids the user has at least one watched episode in.
    Used by the watch-history list to seed candidate shows."""
    rows = (
        (
            await db.execute(
                select(Episode.show_id)
                .join(UserEpisodeWatch, UserEpisodeWatch.episode_id == Episode.id)
                .where(UserEpisodeWatch.user_id == user_id)
                .group_by(Episode.show_id)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def watched_in(
    db: AsyncSession, *, user_id: UUID, episode_ids: list[int] | set[int]
) -> set[int]:
    """Return the subset of `episode_ids` the user has watched. Used by list
    builders to populate `EpisodeOut.watched` in one batch query."""
    if not episode_ids:
        return set()
    rows = (
        (
            await db.execute(
                select(UserEpisodeWatch.episode_id).where(
                    UserEpisodeWatch.user_id == user_id,
                    UserEpisodeWatch.episode_id.in_(episode_ids),
                )
            )
        )
        .scalars()
        .all()
    )
    return set(rows)


async def list_episode_ids_for_show(db: AsyncSession, *, user_id: UUID, show_id: int) -> list[int]:
    rows = (
        (
            await db.execute(
                select(UserEpisodeWatch.episode_id)
                .join(Episode, Episode.id == UserEpisodeWatch.episode_id)
                .where(
                    UserEpisodeWatch.user_id == user_id,
                    Episode.show_id == show_id,
                )
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def watched_count_per_season(
    db: AsyncSession, *, user_id: UUID, show_id: int
) -> dict[int, int]:
    rows = (
        await db.execute(
            select(Episode.season, func.count(UserEpisodeWatch.episode_id))
            .join(UserEpisodeWatch, UserEpisodeWatch.episode_id == Episode.id)
            .where(
                Episode.show_id == show_id,
                UserEpisodeWatch.user_id == user_id,
            )
            .group_by(Episode.season)
        )
    ).all()
    return {season: count for season, count in rows}


async def count_watched_per_show(
    db: AsyncSession, *, user_id: UUID, show_ids: list[int]
) -> dict[int, int]:
    rows = (
        await db.execute(
            select(Episode.show_id, func.count(UserEpisodeWatch.episode_id))
            .join(UserEpisodeWatch, UserEpisodeWatch.episode_id == Episode.id)
            .where(
                Episode.show_id.in_(show_ids),
                UserEpisodeWatch.user_id == user_id,
            )
            .group_by(Episode.show_id)
        )
    ).all()
    return {sid: c for sid, c in rows}


async def latest_watched_per_show(
    db: AsyncSession, *, user_id: UUID, show_ids: list[int]
) -> dict[int, datetime]:
    """Return max(watched_at) per show for the user, restricted to show_ids."""
    rows = (
        await db.execute(
            select(Episode.show_id, func.max(UserEpisodeWatch.watched_at))
            .join(UserEpisodeWatch, UserEpisodeWatch.episode_id == Episode.id)
            .where(
                Episode.show_id.in_(show_ids),
                UserEpisodeWatch.user_id == user_id,
            )
            .group_by(Episode.show_id)
        )
    ).all()
    return {sid: ts for sid, ts in rows}


async def bulk_mark(
    db: AsyncSession,
    *,
    user_id: UUID,
    episode_ids: list[int],
    watched_at: datetime,
) -> None:
    rows = [
        {"user_id": user_id, "episode_id": ep_id, "watched_at": watched_at} for ep_id in episode_ids
    ]
    stmt = pg_insert(UserEpisodeWatch).values(rows)
    stmt = stmt.on_conflict_do_nothing(index_elements=["user_id", "episode_id"])
    await db.execute(stmt)


async def bulk_unmark(
    db: AsyncSession,
    *,
    user_id: UUID,
    episode_ids: list[int],
) -> None:
    await db.execute(
        sa_delete(UserEpisodeWatch).where(
            and_(
                UserEpisodeWatch.user_id == user_id,
                UserEpisodeWatch.episode_id.in_(episode_ids),
            )
        )
    )
