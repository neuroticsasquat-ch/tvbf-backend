from typing import Any
from uuid import UUID

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.models import ActivityEvent
from tvbf.tvmaze.models import Episode


async def upsert(
    session: AsyncSession,
    *,
    actor_id: UUID,
    verb: str,
    target_type: str,
    target_id: int,
    season_number: int | None = None,
    payload: dict[str, Any] | None = None,
) -> ActivityEvent:
    stmt = (
        insert(ActivityEvent)
        .values(
            actor_id=actor_id,
            verb=verb,
            target_type=target_type,
            target_id=target_id,
            season_number=season_number,
            payload=payload,
        )
        .on_conflict_do_update(
            constraint="uq_activity_event",
            set_={"created_at": func.now(), "payload": payload},
        )
        .returning(ActivityEvent)
    )
    result = await session.execute(stmt)
    return result.scalar_one()


async def delete(
    session: AsyncSession,
    *,
    actor_id: UUID,
    verb: str,
    target_type: str,
    target_id: int,
    season_number: int | None = None,
) -> int:
    where = [
        ActivityEvent.actor_id == actor_id,
        ActivityEvent.verb == verb,
        ActivityEvent.target_type == target_type,
        ActivityEvent.target_id == target_id,
    ]
    if season_number is None:
        where.append(ActivityEvent.season_number.is_(None))
    else:
        where.append(ActivityEvent.season_number == season_number)
    result = await session.execute(sa_delete(ActivityEvent).where(*where))
    return result.rowcount  # type: ignore[attr-defined]


async def delete_episode_events_for_season(
    session: AsyncSession, *, actor_id: UUID, show_id: int, season_number: int
) -> int:
    """Delete watched_episode events for this actor whose episode is in the given season."""
    episode_ids_subq = select(Episode.id).where(
        Episode.show_id == show_id, Episode.season == season_number
    )
    result = await session.execute(
        sa_delete(ActivityEvent).where(
            ActivityEvent.actor_id == actor_id,
            ActivityEvent.verb == "watched_episode",
            ActivityEvent.target_type == "episode",
            ActivityEvent.target_id.in_(episode_ids_subq),
        )
    )
    return result.rowcount  # type: ignore[attr-defined]


async def delete_episode_and_season_events_for_show(
    session: AsyncSession, *, actor_id: UUID, show_id: int
) -> int:
    """Delete this actor's watched_season events for the show and any watched_episode
    events whose episode belongs to the show."""
    episode_ids_subq = select(Episode.id).where(Episode.show_id == show_id)
    season_delete = await session.execute(
        sa_delete(ActivityEvent).where(
            ActivityEvent.actor_id == actor_id,
            ActivityEvent.verb == "watched_season",
            ActivityEvent.target_type == "show",
            ActivityEvent.target_id == show_id,
        )
    )
    episode_delete = await session.execute(
        sa_delete(ActivityEvent).where(
            ActivityEvent.actor_id == actor_id,
            ActivityEvent.verb == "watched_episode",
            ActivityEvent.target_type == "episode",
            ActivityEvent.target_id.in_(episode_ids_subq),
        )
    )
    return season_delete.rowcount + episode_delete.rowcount  # type: ignore[attr-defined]
