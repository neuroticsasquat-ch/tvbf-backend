from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from httpx import Request as HRequest
from sqlalchemy.ext.asyncio import AsyncSession

from tests.fixtures.handles import new_handle
from tvbf.app import passwords, tokens
from tvbf.app.models import User
from tvbf.app.repos import invite_repo, session_repo
from tvbf.main import app


@pytest.fixture
async def make_invite(session: AsyncSession):
    """Factory that creates an invite row and returns the code string."""

    async def _make(*, email_hint: str | None = None) -> str:
        code = tokens.new_session_id()  # any URL-safe random; piggybacks on existing helper
        await invite_repo.create(session, code=code, email_hint=email_hint)
        await session.commit()
        return code

    return _make


@pytest.fixture
async def make_user(session: AsyncSession):
    """Factory that creates and returns an `app.user` row, committed.

    `verified` defaults to False because that is what `account_service.signup`
    produces — a factory defaulting to verified would manufacture a state real
    signup never creates. Callers that need to pass the NEU-1161 social gate
    (or to be discoverable in `/users/search`) opt in explicitly.

    `handle` defaults to a random-but-valid value (NEU-1163). It is unique
    because `uq_user_handle` is, and it is *not* derived from `display_name`
    because two users with the default name would then collide — a test wanting
    a specific handle passes one.
    """

    async def _make(
        email: str = "user@example.com",
        password: str = "hunter2hunter2",
        display_name: str = "Test User",
        verified: bool = False,
        handle: str | None = None,
    ) -> User:
        user = User(
            email=email,
            password_hash=passwords.hash_password(password),
            display_name=display_name,
            handle=handle or new_handle(),
            email_verified_at=datetime.now(UTC) if verified else None,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user

    return _make


async def _client_for(session: AsyncSession, user: User) -> AsyncIterator[AsyncClient]:
    """Session row, CSRF token and cookie injection for one user.

    httpx refuses to send cookies whose domain is a single-label TLD (like
    "test"). We work around this by injecting the Cookie header directly via a
    request event hook.
    """
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


@pytest.fixture
async def authed_client(session: AsyncSession, make_user) -> AsyncIterator[AsyncClient]:
    """An AsyncClient with a freshly created **verified** user, valid session,
    and CSRF cookies. It stands for an established logged-in account going
    about its business, so it passes the NEU-1161 social gate."""
    user = await make_user(verified=True)
    async for c in _client_for(session, user):
        yield c


@pytest.fixture
async def unverified_client(session: AsyncSession, make_user) -> AsyncIterator[AsyncClient]:
    """`authed_client`'s sibling for an account that has not verified its
    email — everything ungated still works, the social gate does not."""
    user = await make_user(email="unverified@example.com", display_name="Unverified User")
    async for c in _client_for(session, user):
        yield c
