"""Integration tests for /forgot-password and /reset-password."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from tvbf.app.models import AuthToken, Session, User
from tvbf.app.passwords import verify_password
from tvbf.app.repos import session_repo
from tvbf.app.services import auth_token_service
from tvbf.app.tokens import new_session_id
from tvbf.main import app


@pytest.fixture
async def client(session):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        yield c


# ---------------------------------------------------------------------------
# POST /forgot-password
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_forgot_known_email_issues_token_and_sends(
    client, make_user, session, _stub_outbound_email
):
    user = await make_user(email="alice@example.com")
    r = await client.post("/forgot-password", json={"email": "alice@example.com"})
    assert r.status_code == 202

    rows = await session.execute(select(AuthToken))
    tokens = list(rows.scalars().all())
    assert len(tokens) == 1
    assert tokens[0].user_id == user.id
    assert tokens[0].purpose == auth_token_service.PURPOSE_PASSWORD_RESET

    assert len(_stub_outbound_email) == 1
    sent = _stub_outbound_email[0]
    assert sent["to"] == "alice@example.com"
    assert "/reset-password?token=" in sent["text"]


@pytest.mark.asyncio
async def test_forgot_unknown_email_returns_same_202_silently(
    client, session, _stub_outbound_email
):
    r = await client.post("/forgot-password", json={"email": "nobody@example.com"})
    assert r.status_code == 202

    rows = await session.execute(select(AuthToken))
    assert list(rows.scalars().all()) == []
    assert _stub_outbound_email == []


@pytest.mark.asyncio
async def test_forgot_rate_limited_returns_same_202_and_skips_send(
    client, make_user, session, _stub_outbound_email
):
    await make_user(email="alice@example.com")
    r1 = await client.post("/forgot-password", json={"email": "alice@example.com"})
    assert r1.status_code == 202

    r2 = await client.post("/forgot-password", json={"email": "alice@example.com"})
    assert r2.status_code == 202

    rows = await session.execute(select(AuthToken))
    tokens = list(rows.scalars().all())
    assert len(tokens) == 1  # second attempt was rate-limited
    assert len(_stub_outbound_email) == 1


# ---------------------------------------------------------------------------
# POST /reset-password
# ---------------------------------------------------------------------------


async def _issue_reset_token(session, user_id) -> str:
    issued = await auth_token_service.issue(
        session,
        user_id=user_id,
        purpose=auth_token_service.PURPOSE_PASSWORD_RESET,
    )
    await session.commit()
    return issued.raw_token


@pytest.mark.asyncio
async def test_reset_happy_path_rotates_password_and_revokes_sessions(client, make_user, session):
    user = await make_user(password="oldpassword1234")
    raw = await _issue_reset_token(session, user.id)

    # Seed two existing sessions for this user.
    for _ in range(2):
        await session_repo.create(
            session,
            session_id=new_session_id(),
            user_id=user.id,
            ttl_days=30,
            user_agent=None,
            ip=None,
        )
    await session.commit()

    r = await client.post("/reset-password", json={"token": raw, "new_password": "brandnew12345"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}

    rows = await session.execute(
        select(User).where(User.id == user.id).execution_options(populate_existing=True)
    )
    refreshed = rows.scalar_one()
    assert verify_password("brandnew12345", refreshed.password_hash)
    assert not verify_password("oldpassword1234", refreshed.password_hash)

    sess_rows = await session.execute(select(Session).where(Session.user_id == user.id))
    assert list(sess_rows.scalars().all()) == []


@pytest.mark.asyncio
async def test_reset_invalid_token_returns_400(client):
    r = await client.post(
        "/reset-password", json={"token": "not-real", "new_password": "brandnew12345"}
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "invalid_token"


@pytest.mark.asyncio
async def test_reset_replay_fails(client, make_user, session):
    user = await make_user()
    raw = await _issue_reset_token(session, user.id)

    r1 = await client.post("/reset-password", json={"token": raw, "new_password": "brandnew12345"})
    assert r1.status_code == 200
    r2 = await client.post("/reset-password", json={"token": raw, "new_password": "anothernew123"})
    assert r2.status_code == 400


@pytest.mark.asyncio
async def test_reset_expired_token_fails(client, make_user, session):
    user = await make_user()
    issued = await auth_token_service.issue(
        session,
        user_id=user.id,
        purpose=auth_token_service.PURPOSE_PASSWORD_RESET,
    )
    issued.token.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await session.commit()

    r = await client.post(
        "/reset-password",
        json={"token": issued.raw_token, "new_password": "brandnew12345"},
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_reset_short_password_returns_422(client, make_user, session):
    user = await make_user()
    raw = await _issue_reset_token(session, user.id)
    r = await client.post("/reset-password", json={"token": raw, "new_password": "short"})
    assert r.status_code == 422
