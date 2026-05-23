"""Cookie-session admin invite endpoints (NEU-187).

Distinct from `routers/invites_admin.py`, which is bearer-token gated for
scripting. These routes are addressable from the SPA by admins
(`current_user.is_admin == True`) and email the invitee a signup link with
the code prefilled.
"""

import logging
from urllib.parse import urlencode

from fastapi import APIRouter, Depends
from pydantic import BaseModel, EmailStr
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.models import User
from tvbf.app.schemas import InviteOut
from tvbf.app.services import invite_service
from tvbf.config import Settings, get_settings
from tvbf.deps import get_session, require_admin_user, require_csrf
from tvbf.email import EmailSendError, send_email
from tvbf.email.templates import render_invite_email

log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/invites", tags=["admin"])


class AdminInviteEmailRequest(BaseModel):
    email: EmailStr


def _build_signup_url(*, frontend_base_url: str, code: str, email: str) -> str:
    base = frontend_base_url.rstrip("/")
    return f"{base}/signup?{urlencode({'invite': code, 'email': email})}"


@router.post(
    "/email",
    response_model=InviteOut,
    dependencies=[Depends(require_csrf)],
)
async def create_invite_and_email(
    payload: AdminInviteEmailRequest,
    _admin: User = Depends(require_admin_user),
    db: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> InviteOut:
    email = str(payload.email)
    invite = await invite_service.create_invite(db, email_hint=email)

    signup_url = _build_signup_url(
        frontend_base_url=settings.frontend_base_url, code=invite.code, email=email
    )
    subject, html_body, text_body = render_invite_email(
        code=invite.code, email=email, signup_url=signup_url
    )
    try:
        await send_email(to=email, subject=subject, html=html_body, text=text_body)
    except EmailSendError:
        log.warning("admin_invite.send_failed code=%s", invite.code, exc_info=True)

    return InviteOut(
        code=invite.code,
        email_hint=invite.email_hint,
        created_at=invite.created_at,
        consumed_at=invite.consumed_at,
        consumed_by_user_id=invite.consumed_by_user_id,
    )


@router.get("/cookie", response_model=list[InviteOut])
async def list_invites_cookie(
    _admin: User = Depends(require_admin_user),
    db: AsyncSession = Depends(get_session),
) -> list[InviteOut]:
    invites = await invite_service.list_invites(db)
    return [
        InviteOut(
            code=i.code,
            email_hint=i.email_hint,
            created_at=i.created_at,
            consumed_at=i.consumed_at,
            consumed_by_user_id=i.consumed_by_user_id,
        )
        for i in invites
    ]
