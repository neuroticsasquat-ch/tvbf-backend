from typing import Any
from uuid import UUID

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, not_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.models import ActivityEvent
from tvbf.catalog.episodes import IS_COPIED_SPECIAL, IS_SPECIAL
from tvbf.catalog.models import Episode


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
    """Collapse this actor's watched_episode events for the season into the
    `watched_season` event replacing them.

    Scoped to the episodes the season mark actually covers — the same predicate
    `episode_repo.list_episode_ids_for_season` carries (NEU-1062). A collapse
    wider than the mark deletes the feed item for a copied special somebody
    ticked by hand while leaving its watch row standing, and the `watched_season`
    event substituted for it never covered that episode. §5 of the spec is
    explicit that a special's event still appears in the feed.
    """
    episode_ids_subq = select(Episode.id).where(
        Episode.show_id == show_id,
        Episode.season_number == season_number,
        not_(IS_COPIED_SPECIAL),
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
    """Delete this actor's watched_season events for the show, and collapse the
    watched_episode events the show mark covers.

    Same rule as the season grain one level down, against the predicate
    `episode_repo.list_aired_episode_ids_for_show` carries: bulk-marking a show
    marks no special, so collapsing a special's event into `watched_show` would
    claim a coverage the mark does not have (NEU-1062).
    """
    episode_ids_subq = select(Episode.id).where(Episode.show_id == show_id, not_(IS_SPECIAL))
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
