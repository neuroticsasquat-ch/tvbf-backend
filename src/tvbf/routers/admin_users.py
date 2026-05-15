"""Cookie-session admin user-management endpoints (NEU-185).

Distinct from `routers/admin.py`, which is bearer-token gated for scripting
(ingest, AKA backfill, etc.). These routes are addressable from the SPA by
admins (`current_user.is_admin == True`).
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.models import User
from tvbf.app.schemas import AdminUserOut, AdminUserUpdateRequest
from tvbf.deps import get_session, require_admin_user, require_csrf

router = APIRouter(prefix="/admin/users", tags=["admin"])


@router.get("", response_model=list[AdminUserOut])
async def list_users(
    _admin: User = Depends(require_admin_user),
    db: AsyncSession = Depends(get_session),
) -> list[AdminUserOut]:
    rows = (await db.execute(select(User).order_by(User.created_at))).scalars().all()
    return [
        AdminUserOut(
            id=u.id,
            email=u.email,
            display_name=u.display_name,
            created_at=u.created_at,
            is_admin=u.is_admin,
        )
        for u in rows
    ]


@router.patch(
    "/{user_id}/admin",
    response_model=AdminUserOut,
    dependencies=[Depends(require_csrf)],
)
async def set_admin_flag(
    user_id: Annotated[UUID, Path()],
    payload: AdminUserUpdateRequest,
    admin: User = Depends(require_admin_user),
    db: AsyncSession = Depends(get_session),
) -> AdminUserOut:
    if user_id == admin.id and payload.is_admin is False:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="cannot_demote_self")
    target = await db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user_not_found")
    target.is_admin = payload.is_admin
    await db.commit()
    return AdminUserOut(
        id=target.id,
        email=target.email,
        display_name=target.display_name,
        created_at=target.created_at,
        is_admin=target.is_admin,
    )
