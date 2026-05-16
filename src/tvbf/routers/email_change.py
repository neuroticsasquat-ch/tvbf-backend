"""Email-change HTTP surface.

- POST /me/email/change — authed, CSRF, requires password. Issues a token +
  sends a confirmation link to the new address.
- POST /email-change/confirm — unauthed. Consumes the token and applies the
  new email.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.errors import (
    AuthTokenRateLimited,
    EmailChangePayloadMissing,
    EmailInUse,
    InvalidAuthToken,
    InvalidCredentials,
)
from tvbf.app.models import User
from tvbf.app.schemas import EmailChangeConfirmRequest, EmailChangeRequest
from tvbf.app.services import email_change_service
from tvbf.config import Settings, get_settings
from tvbf.deps import get_current_user, get_session, require_csrf

router = APIRouter(tags=["email-change"])


@router.post(
    "/me/email/change",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_csrf)],
)
async def request_email_change(
    payload: EmailChangeRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Response:
    try:
        await email_change_service.request_email_change(
            db,
            user=user,
            new_email=str(payload.new_email),
            current_password=payload.current_password,
            frontend_base_url=settings.frontend_base_url,
        )
    except InvalidCredentials as err:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_credentials"
        ) from err
    except EmailInUse as err:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="email_in_use") from err
    except AuthTokenRateLimited as err:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="rate_limited"
        ) from err
    return Response(status_code=status.HTTP_202_ACCEPTED)


@router.post("/email-change/confirm", status_code=status.HTTP_200_OK)
async def confirm_email_change(
    payload: EmailChangeConfirmRequest,
    db: AsyncSession = Depends(get_session),
) -> dict[str, bool]:
    try:
        await email_change_service.confirm_email_change(db, raw_token=payload.token)
    except (InvalidAuthToken, EmailChangePayloadMissing) as err:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_token"
        ) from err
    except EmailInUse as err:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="email_in_use") from err
    return {"ok": True}
