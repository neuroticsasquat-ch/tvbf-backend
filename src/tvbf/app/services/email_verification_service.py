"""Email-verification flow: issue a token, send the link, consume it on click.

Sits on top of `auth_token_service` (for tokens) and `tvbf.email` (for transport).
Errors from email transport are caught and logged — they never bubble up as a
500 to the user, since the alternative is leaking transport state.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from urllib.parse import urlencode

from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.errors import AuthTokenRateLimited
from tvbf.app.models import User
from tvbf.app.services import auth_token_service
from tvbf.email import EmailSendError, send_email
from tvbf.email.templates import render_verification_email

log = logging.getLogger(__name__)


def _build_verify_url(*, frontend_base_url: str, token: str) -> str:
    base = frontend_base_url.rstrip("/")
    return f"{base}/verify-email?{urlencode({'token': token})}"


async def send_verification_email(
    db: AsyncSession,
    *,
    user: User,
    frontend_base_url: str,
) -> None:
    """Issue a token (if the rate limit allows) and send the email. Caller must
    have already committed the user row. Raises `AuthTokenRateLimited` when the
    user has issued too many tokens recently; transport failures are swallowed.
    """
    if not await auth_token_service.can_issue(
        db,
        user_id=user.id,
        purpose=auth_token_service.PURPOSE_EMAIL_VERIFICATION,
    ):
        raise AuthTokenRateLimited()

    issued = await auth_token_service.issue(
        db,
        user_id=user.id,
        purpose=auth_token_service.PURPOSE_EMAIL_VERIFICATION,
    )
    await db.commit()

    verify_url = _build_verify_url(frontend_base_url=frontend_base_url, token=issued.raw_token)
    subject, html_body, text_body = render_verification_email(
        display_name=user.display_name, verify_url=verify_url
    )

    try:
        await send_email(to=user.email, subject=subject, html=html_body, text=text_body)
    except EmailSendError:
        log.warning("email_verification.send_failed user_id=%s", user.id, exc_info=True)


async def send_verification_email_best_effort(
    db: AsyncSession,
    *,
    user: User,
    frontend_base_url: str,
) -> None:
    """Same as `send_verification_email` but also swallows rate-limit + unexpected
    errors. Used from the signup path where a failure to send should NOT 500
    the signup itself.
    """
    try:
        await send_verification_email(db, user=user, frontend_base_url=frontend_base_url)
    except AuthTokenRateLimited:
        log.info("email_verification.signup_rate_limited user_id=%s", user.id)
    except Exception:  # pragma: no cover - defensive; covered indirectly
        log.exception("email_verification.signup_unexpected_error user_id=%s", user.id)


async def verify(db: AsyncSession, *, raw_token: str) -> User:
    """Consume the token and mark the user verified. Raises `InvalidAuthToken`
    if the token is unknown, expired, replayed, or for the wrong purpose.
    """
    user = await auth_token_service.verify_and_consume(
        db,
        raw_token=raw_token,
        purpose=auth_token_service.PURPOSE_EMAIL_VERIFICATION,
    )
    if user.email_verified_at is None:
        user.email_verified_at = datetime.now(UTC)
    await db.commit()
    return user
