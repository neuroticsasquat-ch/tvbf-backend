"""Integration tests for the email-verification routes + the signup hook."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from tvbf.app.models import AuthToken, User
from tvbf.app.services import auth_token_service
from tvbf.main import app


@pytest.fixture
async def client(session):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        yield c


# ---------------------------------------------------------------------------
# Signup auto-trigger
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_signup_sends_verification_email(client, make_invite, session, _stub_outbound_email):
    invite = await make_invite()
    r = await client.post(
        "/auth/signup",
        json={
            "email": "alice@example.com",
            "password": "hunter2hunter2",
            "display_name": "Alice",
            "invite_code": invite,
        },
    )
    assert r.status_code == 201
    assert r.json()["email_verified_at"] is None

    assert len(_stub_outbound_email) == 1
    sent = _stub_outbound_email[0]
    assert sent["to"] == "alice@example.com"
    assert "verify-email?token=" in sent["text"]
    assert "verify-email?token=" in sent["html"]

    # And the token row exists.
    rows = await session.execute(select(AuthToken))
    tokens = list(rows.scalars().all())
    assert len(tokens) == 1
    assert tokens[0].purpose == auth_token_service.PURPOSE_EMAIL_VERIFICATION


# ---------------------------------------------------------------------------
# POST /me/email/verification (resend)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_endpoint_issues_a_token_and_sends(
    authed_client, session, _stub_outbound_email
):
    # The authed_client fixture creates a user via the User model directly, so
    # there's no prior signup-triggered token to interfere with the rate limit.
    r = await authed_client.post("/me/email/verification")
    assert r.status_code == 202

    rows = await session.execute(select(AuthToken))
    tokens = list(rows.scalars().all())
    assert len(tokens) == 1
    assert len(_stub_outbound_email) == 1


@pytest.mark.asyncio
async def test_request_endpoint_rate_limits_repeat_calls(
    authed_client, session, _stub_outbound_email
):
    r1 = await authed_client.post("/me/email/verification")
    assert r1.status_code == 202
    r2 = await authed_client.post("/me/email/verification")
    assert r2.status_code == 429
    assert r2.json()["detail"] == "rate_limited"
    # Still only one token + one email despite the second call.
    rows = await session.execute(select(AuthToken))
    assert len(list(rows.scalars().all())) == 1
    assert len(_stub_outbound_email) == 1


# ---------------------------------------------------------------------------
# POST /verify-email (consume)
# ---------------------------------------------------------------------------


async def _issue_email_verification_token(session, user_id) -> str:
    issued = await auth_token_service.issue(
        session,
        user_id=user_id,
        purpose=auth_token_service.PURPOSE_EMAIL_VERIFICATION,
    )
    await session.commit()
    return issued.raw_token


@pytest.mark.asyncio
async def test_verify_happy_path(client, make_user, session):
    user = await make_user()
    raw = await _issue_email_verification_token(session, user.id)

    r = await client.post("/verify-email", json={"token": raw})
    assert r.status_code == 200
    assert r.json() == {"ok": True}

    rows = await session.execute(
        select(User).where(User.id == user.id).execution_options(populate_existing=True)
    )
    refreshed = rows.scalar_one()
    assert refreshed.email_verified_at is not None


@pytest.mark.asyncio
async def test_verify_replay_fails(client, make_user, session):
    user = await make_user()
    raw = await _issue_email_verification_token(session, user.id)

    assert (await client.post("/verify-email", json={"token": raw})).status_code == 200
    r2 = await client.post("/verify-email", json={"token": raw})
    assert r2.status_code == 400
    assert r2.json()["detail"] == "invalid_token"


@pytest.mark.asyncio
async def test_verify_expired_token_fails(client, make_user, session):
    user = await make_user()
    issued = await auth_token_service.issue(
        session,
        user_id=user.id,
        purpose=auth_token_service.PURPOSE_EMAIL_VERIFICATION,
    )
    issued.token.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await session.commit()

    r = await client.post("/verify-email", json={"token": issued.raw_token})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_verify_wrong_purpose_fails(client, make_user, session):
    user = await make_user()
    issued = await auth_token_service.issue(
        session,
        user_id=user.id,
        purpose=auth_token_service.PURPOSE_PASSWORD_RESET,
    )
    await session.commit()

    r = await client.post("/verify-email", json={"token": issued.raw_token})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_verify_unknown_token_fails(client):
    r = await client.post("/verify-email", json={"token": "not-real"})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# GET /me exposes email_verified_at
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_me_response_includes_email_verified_at(authed_client):
    r = await authed_client.get("/me")
    assert r.status_code == 200
    body = r.json()
    assert "email_verified_at" in body
    assert body["email_verified_at"] is None
