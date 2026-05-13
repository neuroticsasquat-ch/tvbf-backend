"""Read-side helpers for the user-facing session list."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

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
