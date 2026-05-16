"""Forgot-password / reset endpoints.

Both are unauthed. `/forgot-password` always returns 202 with the same body —
account existence and rate-limit state are never reflected in the response so
attackers can't enumerate accounts. `/reset-password` consumes the token and
rotates the password.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.errors import InvalidAuthToken
from tvbf.app.schemas import ForgotPasswordRequest, ResetPasswordRequest
from tvbf.app.services import password_reset_service
from tvbf.config import Settings, get_settings
from tvbf.deps import get_session

router = APIRouter(tags=["password-reset"])


@router.post("/forgot-password", status_code=status.HTTP_202_ACCEPTED)
async def forgot_password(
    payload: ForgotPasswordRequest,
    db: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Response:
    await password_reset_service.request_reset(
        db,
        email=str(payload.email),
        frontend_base_url=settings.frontend_base_url,
    )
    return Response(status_code=status.HTTP_202_ACCEPTED)


@router.post("/reset-password", status_code=status.HTTP_200_OK)
async def reset_password(
    payload: ResetPasswordRequest,
    db: AsyncSession = Depends(get_session),
) -> dict[str, bool]:
    try:
        await password_reset_service.reset(
            db, raw_token=payload.token, new_password=payload.new_password
        )
    except InvalidAuthToken as err:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_token"
        ) from err
    return {"ok": True}
