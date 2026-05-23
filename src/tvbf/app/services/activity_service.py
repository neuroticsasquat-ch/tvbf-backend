from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.models import ActivityEvent
from tvbf.app.repos import activity_event_repo


async def emit(
    session: AsyncSession,
    *,
    actor_id: UUID,
    verb: str,
    target_type: str,
    target_id: int,
    season_number: int | None = None,
    payload: dict[str, Any] | None = None,
) -> ActivityEvent:
    return await activity_event_repo.upsert(
        session,
        actor_id=actor_id,
        verb=verb,
        target_type=target_type,
        target_id=target_id,
        season_number=season_number,
        payload=payload,
    )


async def cancel(
    session: AsyncSession,
    *,
    actor_id: UUID,
    verb: str,
    target_type: str,
    target_id: int,
    season_number: int | None = None,
) -> int:
    return await activity_event_repo.delete(
        session,
        actor_id=actor_id,
        verb=verb,
        target_type=target_type,
        target_id=target_id,
        season_number=season_number,
    )


async def collapse_for_season(
    session: AsyncSession, *, actor_id: UUID, show_id: int, season_number: int
) -> int:
    return await activity_event_repo.delete_episode_events_for_season(
        session, actor_id=actor_id, show_id=show_id, season_number=season_number
    )


async def collapse_for_show(session: AsyncSession, *, actor_id: UUID, show_id: int) -> int:
    return await activity_event_repo.delete_episode_and_season_events_for_show(
        session, actor_id=actor_id, show_id=show_id
    )
