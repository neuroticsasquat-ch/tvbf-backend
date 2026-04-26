from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from httpx import Request as HRequest
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app import passwords, tokens
from tvbf.app.models import User
from tvbf.app.repos import session_repo
from tvbf.main import app


@pytest.fixture
async def make_user(session: AsyncSession):
    """Factory that creates and returns an `app.user` row, committed."""

    async def _make(
        email: str = "user@example.com",
        password: str = "hunter2hunter2",
        display_name: str = "Test User",
    ) -> User:
        user = User(
            email=email,
            password_hash=passwords.hash_password(password),
            display_name=display_name,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user

    return _make


@pytest.fixture
async def authed_client(session: AsyncSession, make_user) -> AsyncIterator[AsyncClient]:
    """An AsyncClient with a freshly created user, valid session, and CSRF cookies.

    httpx refuses to send cookies whose domain is a single-label TLD (like "test").
    We work around this by injecting the Cookie header directly via a request event hook.
    """
    user = await make_user()
    sess_id = tokens.new_session_id()
    await session_repo.create(
        session,
        session_id=sess_id,
        user_id=user.id,
        ttl_days=30,
        user_agent=None,
        ip=None,
    )
    csrf = tokens.new_csrf_token()
    await session.commit()

    async def _inject_cookies(request: HRequest) -> None:
        cookie_header = f"tvbf_session={sess_id}; csrf_token={csrf}"
        request.headers["cookie"] = cookie_header

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="https://test",
        headers={"X-CSRF-Token": csrf},
        event_hooks={"request": [_inject_cookies]},
    ) as c:
        c.user = user  # type: ignore[attr-defined]
        yield c
