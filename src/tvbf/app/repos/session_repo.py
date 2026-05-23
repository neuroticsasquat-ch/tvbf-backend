from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import delete as sa_delete
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.models import Session


async def create(
    db: AsyncSession,
    *,
    session_id: str,
    user_id: UUID,
    ttl_days: int,
    user_agent: str | None,
    ip: str | None,
) -> None:
    """Insert a new session row. Caller is responsible for committing."""
    expires_at = datetime.now(UTC) + timedelta(days=ttl_days)
    db.add(
        Session(
            id=session_id,
            user_id=user_id,
            expires_at=expires_at,
            user_agent=user_agent,
            ip=ip,
        )
    )


async def get_active(db: AsyncSession, session_id: str) -> Session | None:
    """Return the session row only if it exists and has not expired."""
    now = datetime.now(UTC)
    result = await db.execute(
        select(Session).where(Session.id == session_id, Session.expires_at > now)
    )
    return result.scalar_one_or_none()


_TOUCH_INTERVAL = timedelta(seconds=60)


async def touch(db: AsyncSession, session_id: str) -> None:
    """Bump `last_seen_at` to now, but at most once per `_TOUCH_INTERVAL` per
    session. Debouncing matters because `get_current_user` runs on every authed
    request, and an unconditional UPDATE would write to `app.session` on every
    page load.
    """
    now = datetime.now(UTC)
    threshold = now - _TOUCH_INTERVAL
    await db.execute(
        update(Session)
        .where(Session.id == session_id, Session.last_seen_at <= threshold)
        .values(last_seen_at=now)
    )


async def list_active_for_user(db: AsyncSession, user_id: UUID) -> list[Session]:
    """All non-expired sessions for the user, ordered most-recently-active first."""
    now = datetime.now(UTC)
    result = await db.execute(
        select(Session)
        .where(Session.user_id == user_id, Session.expires_at > now)
        .order_by(Session.last_seen_at.desc())
    )
    return list(result.scalars().all())


async def delete(db: AsyncSession, session_id: str) -> None:
    await db.execute(sa_delete(Session).where(Session.id == session_id))


async def delete_all_for_user(db: AsyncSession, user_id: UUID) -> None:
    await db.execute(sa_delete(Session).where(Session.user_id == user_id))


async def delete_for_user(db: AsyncSession, *, user_id: UUID, session_id: str) -> int:
    """Delete a single session iff it belongs to `user_id`. Returns the rowcount
    (0 if no row matched). Caller commits."""
    result = await db.execute(
        sa_delete(Session).where(Session.id == session_id, Session.user_id == user_id)
    )
    return result.rowcount or 0  # type: ignore[attr-defined]


async def delete_others_for_user(db: AsyncSession, *, user_id: UUID, except_session_id: str) -> int:
    """Delete every session for `user_id` except the named one. Returns the
    number of rows deleted. Caller commits."""
    result = await db.execute(
        sa_delete(Session).where(Session.user_id == user_id, Session.id != except_session_id)
    )
    return result.rowcount or 0  # type: ignore[attr-defined]
