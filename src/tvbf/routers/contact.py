"""POST /contact — unauthenticated contact form (NEU-1164).

An unauthenticated endpoint that sends email is a spam relay by default, so it
carries the same two protections NEU-1160 put on signup: a Turnstile token and
an IP-keyed throttle.

The check order is load-bearing: the throttle is enforced first, then the
attempt is committed (so a Turnstile rejection still counts against the budget),
then Turnstile is verified, and only then is the email sent. An EmailSendError
logs with warning and the caller still sees 204 — same rule as
feedback_service.submit_feedback.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.services import auth_throttle
from tvbf.client_ip import client_ip
from tvbf.config import Settings, get_settings
from tvbf.deps import get_session
from tvbf.email import EmailSendError, send_email
from tvbf.email.templates import render_contact_notification
from tvbf.integrations import turnstile

log = logging.getLogger(__name__)

router = APIRouter(tags=["contact"])


class ContactIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    email: EmailStr = Field(max_length=254)
    message: str = Field(min_length=1, max_length=5000)
    turnstile_token: str | None = Field(default=None, max_length=2048)


@router.post("/contact", status_code=status.HTTP_204_NO_CONTENT)
async def submit_contact(
    payload: ContactIn,
    request: Request,
    db: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Response:
    ip = client_ip(request, trusted_proxy_hops=settings.trusted_proxy_hops)

    # 1. Enforce the IP throttle
    try:
        await auth_throttle.enforce(
            db, kind=auth_throttle.CONTACT, ip=ip, throttle=settings.contact_ip_throttle
        )
    except auth_throttle.TooManyAttempts as err:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="rate_limited",
            headers={"Retry-After": str(err.retry_after_seconds)},
        ) from err

    # 2. Record the attempt and commit — load-bearing, so a Turnstile rejection
    #    still counts against the throttle
    await auth_throttle.record(db, kind=auth_throttle.CONTACT, ip=ip)

    # 3. Verify the Turnstile token (no-op when disabled)
    if settings.turnstile_enabled:
        if not payload.turnstile_token:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="captcha_required")
        secret = settings.turnstile_secret_key or ""
        try:
            await turnstile.verify(token=payload.turnstile_token, secret=secret, remote_ip=ip)
        except turnstile.TurnstileRejected as err:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="captcha_invalid"
            ) from err
        except turnstile.TurnstileUnavailable as err:
            log.warning("turnstile verification unavailable", exc_info=err)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="captcha_unavailable"
            ) from err

    # 4. Compose and send the email
    if settings.feedback_notify_email:
        subject, html, text = render_contact_notification(
            name=payload.name, email=payload.email, message=payload.message
        )
        try:
            await send_email(
                to=settings.feedback_notify_email,
                subject=subject,
                html=html,
                text=text,
                reply_to=payload.email,
            )
        except EmailSendError:
            log.warning(
                "contact notification send failed name=%s email=%s",
                payload.name,
                payload.email,
                exc_info=True,
            )

    return Response(status_code=status.HTTP_204_NO_CONTENT)
