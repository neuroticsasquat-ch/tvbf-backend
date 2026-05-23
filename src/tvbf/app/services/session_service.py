"""Read-side helpers for the user-facing session list."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.errors import NotFound
from tvbf.app.repos import session_repo
from tvbf.app.schemas import SessionSummary
from tvbf.app.user_agent import parse_device_label


async def list_for_user(
    db: AsyncSession,
    *,
    user_id: UUID,
    current_session_id: str | None,
) -> list[SessionSummary]:
    rows = await session_repo.list_active_for_user(db, user_id)
    return [
        SessionSummary(
            id=row.id,
            device_label=parse_device_label(row.user_agent),
            ip=str(row.ip) if row.ip is not None else None,
            last_seen_at=row.last_seen_at,
            created_at=row.created_at,
            is_current=(row.id == current_session_id),
        )
        for row in rows
    ]


async def revoke(db: AsyncSession, *, user_id: UUID, session_id: str) -> None:
    """Delete a session belonging to `user_id`. Raises NotFound if the row
    doesn't exist or belongs to someone else."""
    deleted = await session_repo.delete_for_user(db, user_id=user_id, session_id=session_id)
    if deleted == 0:
        raise NotFound()
    await db.commit()


async def revoke_others(db: AsyncSession, *, user_id: UUID, current_session_id: str) -> int:
    """Delete every session for `user_id` except `current_session_id`. Returns
    the count of revoked sessions."""
    revoked = await session_repo.delete_others_for_user(
        db, user_id=user_id, except_session_id=current_session_id
    )
    await db.commit()
    return revoked
