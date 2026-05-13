"""One-time bearer tokens for email verification and password reset.
Repo: pure DB I/O, no commits, no business rules."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.models import AuthToken


async def create(
    db: AsyncSession,
    *,
    user_id: UUID,
    token_hash: str,
    purpose: str,
    expires_at: datetime,
) -> AuthToken:
    token = AuthToken(
        user_id=user_id,
        token_hash=token_hash,
        purpose=purpose,
        expires_at=expires_at,
    )
    db.add(token)
    await db.flush()
    return token


async def find_unconsumed_by_hash(
    db: AsyncSession, *, token_hash: str, purpose: str
) -> AuthToken | None:
    rows = await db.execute(
        select(AuthToken).where(
            AuthToken.token_hash == token_hash,
            AuthToken.purpose == purpose,
            AuthToken.consumed_at.is_(None),
        )
    )
    return rows.scalar_one_or_none()


async def consume(db: AsyncSession, *, token: AuthToken, consumed_at: datetime) -> None:
    """Mark a token consumed. Caller already has the row loaded."""
    token.consumed_at = consumed_at
    await db.flush()


async def count_recent_for_user(
    db: AsyncSession, *, user_id: UUID, purpose: str, since: datetime
) -> int:
    rows = await db.execute(
        select(func.count())
        .select_from(AuthToken)
        .where(
            AuthToken.user_id == user_id,
            AuthToken.purpose == purpose,
            AuthToken.created_at >= since,
        )
    )
    return int(rows.scalar_one())
