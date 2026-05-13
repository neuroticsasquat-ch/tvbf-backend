"""Email-change flow.

Request: signed-in user supplies current password + new email. We re-verify
the password, check that the new address isn't already taken, issue an
`email_change` token carrying `{"new_email": ...}` as its payload, and send a
confirmation link to the **new** address.

Confirm: anyone holding the token can swap their email; the address is
re-checked at confirm time in case another account claimed it in the meantime.
On success we also stamp `email_verified_at` (clicking the link IS proof of
ownership of the new address).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from urllib.parse import urlencode

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.errors import (
    AuthTokenRateLimited,
    EmailChangePayloadMissing,
    EmailInUse,
    InvalidCredentials,
)
from tvbf.app.models import User
from tvbf.app.passwords import verify_password
from tvbf.app.repos import user_repo
from tvbf.app.services import auth_token_service
from tvbf.email import EmailSendError, send_email
from tvbf.email.templates import render_email_change_email

log = logging.getLogger(__name__)


def _build_confirm_url(*, frontend_base_url: str, token: str) -> str:
    base = frontend_base_url.rstrip("/")
    return f"{base}/email-change/confirm?{urlencode({'token': token})}"


async def request_email_change(
    db: AsyncSession,
    *,
    user: User,
    new_email: str,
    current_password: str,
    frontend_base_url: str,
) -> None:
    """Validate inputs, issue an `email_change` token, send the confirmation
    email to the new address.

    Raises:
        InvalidCredentials — wrong password.
        EmailInUse — another user already owns `new_email`.
        AuthTokenRateLimited — the user has issued too many tokens recently.
    """
    if not verify_password(current_password, user.password_hash):
        raise InvalidCredentials()

    existing = await user_repo.get_by_email(db, new_email)
    if existing is not None and existing.id != user.id:
        raise EmailInUse()

    if not await auth_token_service.can_issue(
        db, user_id=user.id, purpose=auth_token_service.PURPOSE_EMAIL_CHANGE
    ):
        raise AuthTokenRateLimited()

    issued = await auth_token_service.issue(
        db,
        user_id=user.id,
        purpose=auth_token_service.PURPOSE_EMAIL_CHANGE,
        payload={"new_email": new_email},
    )
    await db.commit()

    confirm_url = _build_confirm_url(frontend_base_url=frontend_base_url, token=issued.raw_token)
    subject, html_body, text_body = render_email_change_email(
        display_name=user.display_name,
        new_email=new_email,
        confirm_url=confirm_url,
    )
    try:
        await send_email(to=new_email, subject=subject, html=html_body, text=text_body)
    except EmailSendError:
        log.warning("email_change.send_failed user_id=%s", user.id, exc_info=True)


async def confirm_email_change(db: AsyncSession, *, raw_token: str) -> User:
    """Consume the token, swap the email, mark verified.

    Raises:
        InvalidAuthToken — unknown/expired/replayed/wrong-purpose (bubbles).
        EmailChangePayloadMissing — token row had no payload (shouldn't happen).
        EmailInUse — the new address was claimed between request and confirm.
    """
    user, token = await auth_token_service.verify_and_consume_with_token(
        db, raw_token=raw_token, purpose=auth_token_service.PURPOSE_EMAIL_CHANGE
    )
    if not token.payload or "new_email" not in token.payload:
        raise EmailChangePayloadMissing()

    new_email = str(token.payload["new_email"])
    user.email = new_email
    user.email_verified_at = datetime.now(UTC)
    try:
        await db.commit()
    except IntegrityError as err:
        await db.rollback()
        raise EmailInUse() from err
    return user
