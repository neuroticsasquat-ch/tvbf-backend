"""Integration tests for FastAPI dependencies that require a real DB.

- get_current_user — requires session fixture (real Postgres)
"""

import pytest
from fastapi import HTTPException, Request

from tvbf.app.services import account_service
from tvbf.config import get_settings
from tvbf.deps import get_current_user


def _request_with_cookies(cookies: dict[str, str]) -> Request:
    """Build a minimal ASGI Request with the given cookies."""
    cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
    headers: list[tuple[bytes, bytes]] = []
    if cookie_header:
        headers.append((b"cookie", cookie_header.encode()))
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": headers,
        "query_string": b"",
    }
    return Request(scope)


@pytest.mark.asyncio
async def test_get_current_user_raises_401_when_no_session_cookie(session):
    """Direct dep invocation: no cookie → resolve_session_user returns None →
    get_current_user raises 401 with detail='auth_required'."""
    request = _request_with_cookies({})
    settings = get_settings()
    with pytest.raises(HTTPException) as ei:
        await get_current_user(request, db=session, settings=settings)
    assert ei.value.status_code == 401
    assert ei.value.detail == "auth_required"


@pytest.mark.asyncio
async def test_get_current_user_raises_401_for_unknown_session(session):
    request = _request_with_cookies({"tvbf_session": "does-not-exist"})
    settings = get_settings()
    with pytest.raises(HTTPException) as ei:
        await get_current_user(request, db=session, settings=settings)
    assert ei.value.status_code == 401


@pytest.mark.asyncio
async def test_get_current_user_returns_user_for_valid_session(session, make_user):
    user = await make_user(email="dep@example.com")
    _, sess_id, _ = await account_service.authenticate(
        session,
        email="dep@example.com",
        password="hunter2hunter2",
        ttl_days=30,
        user_agent=None,
        ip=None,
    )
    request = _request_with_cookies({"tvbf_session": sess_id})
    settings = get_settings()
    resolved = await get_current_user(request, db=session, settings=settings)
    assert resolved.id == user.id
