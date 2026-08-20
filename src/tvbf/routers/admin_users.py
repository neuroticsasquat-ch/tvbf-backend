"""Cookie-session admin user-management endpoints (NEU-185).

Distinct from `routers/admin.py`, which is bearer-token gated for scripting
(ingest, AKA backfill, etc.). These routes are addressable from the SPA by
admins (`current_user.is_admin == True`).
"""

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.models import User
from tvbf.app.repos import session_repo
from tvbf.app.schemas import (
    AdminUserDisabledUpdateRequest,
    AdminUserOut,
    AdminUserUpdateRequest,
)
from tvbf.deps import get_session, require_admin_user, require_csrf

router = APIRouter(prefix="/admin/users", tags=["admin"])


def _out(u: User) -> AdminUserOut:
    return AdminUserOut(
        id=u.id,
        email=u.email,
        display_name=u.display_name,
        created_at=u.created_at,
        is_admin=u.is_admin,
        disabled_at=u.disabled_at,
    )


@router.get("", response_model=list[AdminUserOut])
async def list_users(
    _admin: User = Depends(require_admin_user),
    db: AsyncSession = Depends(get_session),
) -> list[AdminUserOut]:
    rows = (await db.execute(select(User).order_by(User.created_at))).scalars().all()
    return [_out(u) for u in rows]


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
    return _out(target)


@router.patch(
    "/{user_id}/disabled",
    response_model=AdminUserOut,
    dependencies=[Depends(require_csrf)],
)
async def set_disabled_flag(
    user_id: Annotated[UUID, Path()],
    payload: AdminUserDisabledUpdateRequest,
    admin: User = Depends(require_admin_user),
    db: AsyncSession = Depends(get_session),
) -> AdminUserOut:
    """Set or clear `disabled_at` (NEU-1162 §7.1).

    Three edge behaviours, each deliberate:

    * **Re-disabling someone already disabled leaves the stamp untouched.** It
      records when moderation began, and §1.1 made it the only record of the
      act — re-stamping is the one way to destroy the fact of when it happened.
    * **Session revocation runs whenever `disabled` is true**, not only on the
      transition. Two lines, and it closes the race where a session was minted
      between the flag being set and the delete landing.
    * **An admin may disable another admin.** The self-guard covers the mistake
      worth preventing; blocking this would protect a rogue admin from the only
      remedy that exists.

    Nothing cascades. The whole point is that this is reversible where
    `DELETE /me` is not — clearing the flag restores the account exactly, minus
    the sessions, and the user logs in again.
    """
    if user_id == admin.id and payload.disabled:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="cannot_disable_self")
    target = await db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user_not_found")
    if payload.disabled:
        if target.disabled_at is None:
            target.disabled_at = datetime.now(UTC)
        await session_repo.delete_all_for_user(db, target.id)
    else:
        target.disabled_at = None
    await db.commit()
    return _out(target)
