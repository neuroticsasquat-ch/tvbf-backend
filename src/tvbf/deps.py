from collections.abc import AsyncIterator

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.models import User
from tvbf.app.services import account_service
from tvbf.config import Settings, get_settings
from tvbf.db import SessionLocal


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


def require_admin(
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    if token != settings.admin_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token")


async def require_csrf(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> None:
    cookie = request.cookies.get(settings.csrf_cookie_name)
    header = request.headers.get("X-CSRF-Token")
    if not cookie or not header or cookie != header:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="csrf_invalid")


async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> User:
    sess_id = request.cookies.get(settings.session_cookie_name)
    user = await account_service.resolve_session_user(db, session_id=sess_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="auth_required")
    return user


def require_admin_user(user: User = Depends(get_current_user)) -> User:
    """Cookie-session admin gate. Distinct from `require_admin` (bearer-token)."""
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin_required")
    return user
