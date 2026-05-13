"""Integration tests for session-revocation endpoints."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from tvbf.app.models import Session
from tvbf.app.repos import session_repo
from tvbf.app.tokens import new_session_id
from tvbf.main import app

# ---------------------------------------------------------------------------
# DELETE /me/sessions/{id}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revoke_own_other_session_deletes_only_that_row(authed_client, session):
    me = authed_client.user
    other_id = new_session_id()
    await session_repo.create(
        session,
        session_id=other_id,
        user_id=me.id,
        ttl_days=30,
        user_agent=None,
        ip=None,
    )
    await session.commit()

    r = await authed_client.delete(f"/me/sessions/{other_id}")
    assert r.status_code == 204
    # Current session's auth cookies stay intact.
    cookies = {c.name: c.value for c in r.cookies.jar}
    assert "tvbf_session" not in cookies

    # Row is gone; the current session row is still there.
    rows = await session.execute(select(Session).where(Session.user_id == me.id))
    remaining = [row.id for row in rows.scalars().all()]
    assert other_id not in remaining
    assert len(remaining) == 1


@pytest.mark.asyncio
async def test_revoke_current_session_clears_auth_cookies(authed_client, session):
    me = authed_client.user
    rows = await session.execute(select(Session).where(Session.user_id == me.id))
    [current_row] = list(rows.scalars().all())
    current_id = current_row.id

    r = await authed_client.delete(f"/me/sessions/{current_id}")
    assert r.status_code == 204
    # Both cookies should be cleared (max-age=0 in the response).
    set_cookie_headers = r.headers.get_list("set-cookie")
    assert any("tvbf_session=" in h and "Max-Age=0" in h for h in set_cookie_headers)
    assert any("csrf_token=" in h and "Max-Age=0" in h for h in set_cookie_headers)

    rows = await session.execute(select(Session).where(Session.user_id == me.id))
    assert list(rows.scalars().all()) == []


@pytest.mark.asyncio
async def test_revoke_another_users_session_returns_404(authed_client, make_user, session):
    other_user = await make_user(email="other@example.com")
    foreign_id = new_session_id()
    await session_repo.create(
        session,
        session_id=foreign_id,
        user_id=other_user.id,
        ttl_days=30,
        user_agent=None,
        ip=None,
    )
    await session.commit()

    r = await authed_client.delete(f"/me/sessions/{foreign_id}")
    assert r.status_code == 404
    assert r.json()["detail"] == "not_found"

    # The other user's row must still be there.
    rows = await session.execute(select(Session).where(Session.id == foreign_id))
    assert rows.scalar_one() is not None


@pytest.mark.asyncio
async def test_revoke_unknown_session_returns_404(authed_client):
    r = await authed_client.delete("/me/sessions/does-not-exist")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_revoke_requires_csrf(authed_client):
    r = await authed_client.delete("/me/sessions/whatever", headers={"X-CSRF-Token": ""})
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_revoke_requires_auth():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        r = await c.delete("/me/sessions/whatever")
    assert r.status_code in (401, 403)


# ---------------------------------------------------------------------------
# POST /me/sessions/revoke-others
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revoke_others_leaves_current_session_alone(authed_client, session):
    me = authed_client.user
    # Seed three more sessions for me.
    for _ in range(3):
        await session_repo.create(
            session,
            session_id=new_session_id(),
            user_id=me.id,
            ttl_days=30,
            user_agent=None,
            ip=None,
        )
    await session.commit()

    r = await authed_client.post("/me/sessions/revoke-others")
    assert r.status_code == 200
    assert r.json() == {"revoked": 3}

    rows = await session.execute(select(Session).where(Session.user_id == me.id))
    remaining = list(rows.scalars().all())
    assert len(remaining) == 1  # the authed_client's current session


@pytest.mark.asyncio
async def test_revoke_others_doesnt_touch_other_users(authed_client, make_user, session):
    other_user = await make_user(email="other@example.com")
    foreign_id = new_session_id()
    await session_repo.create(
        session,
        session_id=foreign_id,
        user_id=other_user.id,
        ttl_days=30,
        user_agent=None,
        ip=None,
    )
    await session.commit()

    r = await authed_client.post("/me/sessions/revoke-others")
    assert r.status_code == 200
    assert r.json() == {"revoked": 0}

    # The other user's row is still there.
    rows = await session.execute(select(Session).where(Session.id == foreign_id))
    assert rows.scalar_one() is not None
