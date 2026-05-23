"""Integration tests for the email-change routes."""

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
# POST /me/email/change
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_happy_path_issues_token_and_sends_to_new_address(
    authed_client, session, _stub_outbound_email
):
    r = await authed_client.post(
        "/me/email/change",
        json={"new_email": "alice-new@example.com", "current_password": "hunter2hunter2"},
    )
    assert r.status_code == 202

    rows = await session.execute(select(AuthToken))
    tokens = list(rows.scalars().all())
    assert len(tokens) == 1
    assert tokens[0].purpose == auth_token_service.PURPOSE_EMAIL_CHANGE
    assert tokens[0].payload == {"new_email": "alice-new@example.com"}

    assert len(_stub_outbound_email) == 1
    sent = _stub_outbound_email[0]
    assert sent["to"] == "alice-new@example.com"
    assert "alice-new@example.com" in sent["text"]
    assert "/email-change/confirm?token=" in sent["text"]


@pytest.mark.asyncio
async def test_request_wrong_password_returns_401(authed_client, _stub_outbound_email):
    r = await authed_client.post(
        "/me/email/change",
        json={"new_email": "new@example.com", "current_password": "wrong-password"},
    )
    assert r.status_code == 401
    assert r.json()["detail"] == "invalid_credentials"
    assert _stub_outbound_email == []


@pytest.mark.asyncio
async def test_request_rejects_email_already_used(authed_client, make_user, _stub_outbound_email):
    # Another user owns this email.
    await make_user(email="taken@example.com")
    r = await authed_client.post(
        "/me/email/change",
        json={"new_email": "taken@example.com", "current_password": "hunter2hunter2"},
    )
    assert r.status_code == 409
    assert r.json()["detail"] == "email_in_use"
    assert _stub_outbound_email == []


@pytest.mark.asyncio
async def test_request_rate_limits_repeat_calls(authed_client, _stub_outbound_email):
    r1 = await authed_client.post(
        "/me/email/change",
        json={"new_email": "alice-new@example.com", "current_password": "hunter2hunter2"},
    )
    assert r1.status_code == 202
    r2 = await authed_client.post(
        "/me/email/change",
        json={"new_email": "alice-new2@example.com", "current_password": "hunter2hunter2"},
    )
    assert r2.status_code == 429
    assert r2.json()["detail"] == "rate_limited"
    assert len(_stub_outbound_email) == 1


@pytest.mark.asyncio
async def test_request_requires_csrf(authed_client):
    # Strip the CSRF header to confirm CSRF guard is wired in.
    r = await authed_client.post(
        "/me/email/change",
        json={"new_email": "x@example.com", "current_password": "hunter2hunter2"},
        headers={"X-CSRF-Token": ""},
    )
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# POST /email-change/confirm
# ---------------------------------------------------------------------------


async def _issue_change_token(session, user_id, new_email: str) -> str:
    issued = await auth_token_service.issue(
        session,
        user_id=user_id,
        purpose=auth_token_service.PURPOSE_EMAIL_CHANGE,
        payload={"new_email": new_email},
    )
    await session.commit()
    return issued.raw_token


@pytest.mark.asyncio
async def test_confirm_happy_path_swaps_email_and_marks_verified(client, make_user, session):
    user = await make_user(email="old@example.com")
    raw = await _issue_change_token(session, user.id, "new@example.com")

    r = await client.post("/email-change/confirm", json={"token": raw})
    assert r.status_code == 200
    assert r.json() == {"ok": True}

    rows = await session.execute(
        select(User).where(User.id == user.id).execution_options(populate_existing=True)
    )
    refreshed = rows.scalar_one()
    assert refreshed.email == "new@example.com"
    assert refreshed.email_verified_at is not None


@pytest.mark.asyncio
async def test_confirm_rejects_email_taken_between_request_and_confirm(client, make_user, session):
    user = await make_user(email="old@example.com")
    raw = await _issue_change_token(session, user.id, "race@example.com")
    # Another account snags the address before confirm fires.
    await make_user(email="race@example.com")

    r = await client.post("/email-change/confirm", json={"token": raw})
    assert r.status_code == 409
    assert r.json()["detail"] == "email_in_use"

    rows = await session.execute(
        select(User).where(User.id == user.id).execution_options(populate_existing=True)
    )
    refreshed = rows.scalar_one()
    assert refreshed.email == "old@example.com"


@pytest.mark.asyncio
async def test_confirm_replay_fails(client, make_user, session):
    user = await make_user(email="old@example.com")
    raw = await _issue_change_token(session, user.id, "new@example.com")

    r1 = await client.post("/email-change/confirm", json={"token": raw})
    assert r1.status_code == 200
    r2 = await client.post("/email-change/confirm", json={"token": raw})
    assert r2.status_code == 400
    assert r2.json()["detail"] == "invalid_token"


@pytest.mark.asyncio
async def test_confirm_expired_token_fails(client, make_user, session):
    user = await make_user(email="old@example.com")
    issued = await auth_token_service.issue(
        session,
        user_id=user.id,
        purpose=auth_token_service.PURPOSE_EMAIL_CHANGE,
        payload={"new_email": "new@example.com"},
    )
    issued.token.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await session.commit()

    r = await client.post("/email-change/confirm", json={"token": issued.raw_token})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_confirm_wrong_purpose_fails(client, make_user, session):
    user = await make_user(email="old@example.com")
    issued = await auth_token_service.issue(
        session,
        user_id=user.id,
        purpose=auth_token_service.PURPOSE_EMAIL_VERIFICATION,
    )
    await session.commit()

    r = await client.post("/email-change/confirm", json={"token": issued.raw_token})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_confirm_unknown_token_fails(client):
    r = await client.post("/email-change/confirm", json={"token": "not-a-token"})
    assert r.status_code == 400
