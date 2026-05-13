"""Forgot-password / reset flow.

`request_reset` is the unauthed entry point. It returns the same outcome to
the caller whether or not the email is known — the route returns 202 regardless,
so we encode "this email isn't real" / "you've been rate-limited" as silent
no-ops here.

`reset` consumes the token, rotates the password, and revokes every existing
session so a stolen-but-not-yet-used password can't keep a session alive.
"""

from __future__ import annotations

import logging
from urllib.parse import urlencode

from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.models import User
from tvbf.app.passwords import hash_password
from tvbf.app.repos import session_repo, user_repo
from tvbf.app.services import auth_token_service
from tvbf.email import EmailSendError, send_email
from tvbf.email.templates import render_password_reset_email

log = logging.getLogger(__name__)


def _build_reset_url(*, frontend_base_url: str, token: str) -> str:
    base = frontend_base_url.rstrip("/")
    return f"{base}/reset-password?{urlencode({'token': token})}"


async def request_reset(
    db: AsyncSession,
    *,
    email: str,
    frontend_base_url: str,
) -> None:
    """Best-effort: look up the account, issue a token if the rate limit allows,
    send the email. Silently no-ops if the email is unknown or rate-limited —
    callers (the route) return 202 in every case to avoid account enumeration.
    """
    user = await user_repo.get_by_email(db, email)
    if user is None:
        return

    if not await auth_token_service.can_issue(
        db,
        user_id=user.id,
        purpose=auth_token_service.PURPOSE_PASSWORD_RESET,
    ):
        log.info("password_reset.rate_limited user_id=%s", user.id)
        return

    issued = await auth_token_service.issue(
        db,
        user_id=user.id,
        purpose=auth_token_service.PURPOSE_PASSWORD_RESET,
    )
    await db.commit()

    reset_url = _build_reset_url(frontend_base_url=frontend_base_url, token=issued.raw_token)
    subject, html_body, text_body = render_password_reset_email(
        display_name=user.display_name, reset_url=reset_url
    )
    try:
        await send_email(to=user.email, subject=subject, html=html_body, text=text_body)
    except EmailSendError:
        log.warning("password_reset.send_failed user_id=%s", user.id, exc_info=True)


async def reset(db: AsyncSession, *, raw_token: str, new_password: str) -> User:
    """Consume the token, hash + store the new password, revoke all sessions.
    Raises InvalidAuthToken on a bad/expired/replayed/wrong-purpose token.
    """
    user = await auth_token_service.verify_and_consume(
        db, raw_token=raw_token, purpose=auth_token_service.PURPOSE_PASSWORD_RESET
    )
    await user_repo.update_password_hash(db, user, hash_password(new_password))
    await session_repo.delete_all_for_user(db, user.id)
    await db.commit()
    return user
