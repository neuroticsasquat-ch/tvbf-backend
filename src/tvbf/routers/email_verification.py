"""Email-verification HTTP surface.

- POST /me/email/verification — authed, CSRF. Issues a token + sends the email
  for the current user; 429 if the user has hit the issue rate limit.
- POST /verify-email — unauthed. Consumes a token from the email link and
  marks the user verified.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.errors import AuthTokenRateLimited, InvalidAuthToken
from tvbf.app.models import User
from tvbf.app.schemas import VerifyEmailRequest
from tvbf.app.services import email_verification_service
from tvbf.config import Settings, get_settings
from tvbf.deps import get_current_user, get_session, require_csrf

router = APIRouter(tags=["email-verification"])


@router.post(
    "/me/email/verification",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_csrf)],
)
async def request_email_verification(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Response:
    try:
        await email_verification_service.send_verification_email(
            db, user=user, frontend_base_url=settings.frontend_base_url
        )
    except AuthTokenRateLimited as err:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="rate_limited"
        ) from err
    return Response(status_code=status.HTTP_202_ACCEPTED)


@router.post("/verify-email", status_code=status.HTTP_200_OK)
async def verify_email(
    payload: VerifyEmailRequest,
    db: AsyncSession = Depends(get_session),
) -> dict[str, bool]:
    try:
        await email_verification_service.verify(db, raw_token=payload.token)
    except InvalidAuthToken as err:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_token"
        ) from err
    return {"ok": True}
