"""One-time-token service for email verification and password reset.

Tokens are stored hashed (sha256) — the raw token is returned to the caller
exactly once at issue time so it can be sent in an email or URL. Verification
matches by hash, enforces expiry and purpose, and consumes the row in a single
step so a leaked link can't be replayed.

Rate limits per (user, purpose): 1 token / minute and 5 tokens / hour. Callers
should check `can_issue` before `issue` and surface a friendly error.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.errors import InvalidAuthToken, NotFound
from tvbf.app.models import AuthToken, User
from tvbf.app.repos import auth_token_repo

PURPOSE_EMAIL_VERIFICATION = "email_verification"
PURPOSE_PASSWORD_RESET = "password_reset"
PURPOSE_EMAIL_CHANGE = "email_change"

_TTLS: dict[str, timedelta] = {
    PURPOSE_EMAIL_VERIFICATION: timedelta(hours=24),
    PURPOSE_PASSWORD_RESET: timedelta(hours=1),
    PURPOSE_EMAIL_CHANGE: timedelta(hours=24),
}

RATE_LIMIT_PER_MINUTE = 1
RATE_LIMIT_PER_HOUR = 5


@dataclass(frozen=True)
class IssuedToken:
    raw_token: str
    token: AuthToken


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


def ttl_for(purpose: str) -> timedelta:
    try:
        return _TTLS[purpose]
    except KeyError as e:
        raise ValueError(f"unknown auth token purpose: {purpose}") from e


async def can_issue(db: AsyncSession, *, user_id, purpose: str) -> bool:
    """Enforce 1/min and 5/hour per (user, purpose)."""
    now = _now()
    per_minute = await auth_token_repo.count_recent_for_user(
        db, user_id=user_id, purpose=purpose, since=now - timedelta(minutes=1)
    )
    if per_minute >= RATE_LIMIT_PER_MINUTE:
        return False
    per_hour = await auth_token_repo.count_recent_for_user(
        db, user_id=user_id, purpose=purpose, since=now - timedelta(hours=1)
    )
    return per_hour < RATE_LIMIT_PER_HOUR


async def issue(
    db: AsyncSession,
    *,
    user_id,
    purpose: str,
    payload: dict | None = None,
) -> IssuedToken:
    """Generate a fresh token, store its hash, return the raw value exactly once.

    Callers are responsible for checking `can_issue` first. `payload` is an
    optional jsonb blob that travels with the token (e.g. the pending new email
    for the email-change flow).
    """
    raw = secrets.token_urlsafe(32)
    expires_at = _now() + ttl_for(purpose)
    token = await auth_token_repo.create(
        db,
        user_id=user_id,
        token_hash=_hash(raw),
        purpose=purpose,
        expires_at=expires_at,
        payload=payload,
    )
    return IssuedToken(raw_token=raw, token=token)


async def verify_and_consume(db: AsyncSession, *, raw_token: str, purpose: str) -> User:
    """Match by hash, enforce purpose + expiry, mark consumed, return the user.

    Raises InvalidAuthToken on any mismatch (unknown, wrong purpose, expired,
    already consumed). Raises NotFound if the user behind the token no longer
    exists (cascade should normally prevent this, but we guard anyway).
    """
    user, _ = await verify_and_consume_with_token(db, raw_token=raw_token, purpose=purpose)
    return user


async def verify_and_consume_with_token(
    db: AsyncSession, *, raw_token: str, purpose: str
) -> tuple[User, AuthToken]:
    """Same checks as `verify_and_consume`, but also returns the AuthToken so
    callers can read `payload` (used by the email-change flow)."""
    token = await auth_token_repo.find_unconsumed_by_hash(
        db, token_hash=_hash(raw_token), purpose=purpose
    )
    if token is None:
        raise InvalidAuthToken()
    if token.expires_at <= _now():
        raise InvalidAuthToken()

    user = await db.get(User, token.user_id)
    if user is None:
        raise NotFound()

    await auth_token_repo.consume(db, token=token, consumed_at=_now())
    return user, token
