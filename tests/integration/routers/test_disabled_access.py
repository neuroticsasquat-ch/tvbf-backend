"""What a disabled account can no longer do (NEU-1162 §§2-3).

One predicate in `account_service.resolve_session_user` covers every
session-bearing route at once, and the three emailed-link paths — which take a
token instead of a session, so that predicate never runs on them — each close
themselves with their own existing generic failure.
"""

from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from tvbf.app.models import AuthAttempt, LoginAttempt
from tvbf.app.repos import login_attempt_repo
from tvbf.app.services import auth_token_service
from tvbf.main import app

PASSWORD = "hunter2hunter2"


async def _disable(session, user) -> None:
    user.disabled_at = datetime.now(UTC)
    await session.commit()


# ---------------------------------------------------------------------------
# §2.1 / AC 2 — the session predicate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/shows", "/me", "/admin/users"])
@pytest.mark.asyncio
async def test_disabled_session_is_401_everywhere(authed_client, session, path):
    """A browse route, a `/me` route and an admin route alike. The admin route
    is in the list because a disabled admin must lose admin too — the check is
    upstream of `require_admin_user`, not beside it."""
    me = authed_client.user  # type: ignore[attr-defined]
    me.is_admin = True
    await _disable(session, me)

    r = await authed_client.get(path)
    assert r.status_code == 401
    assert r.json()["detail"] == "auth_required"


@pytest.mark.asyncio
async def test_disabled_response_is_identical_to_no_cookie_at_all(authed_client, session):
    """§2.2: no new status code, no `account_disabled` detail. A distinct
    refusal would be a machine-readable confirmation, one per retry."""
    me = authed_client.user  # type: ignore[attr-defined]
    await _disable(session, me)

    disabled = await authed_client.get("/me")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        anonymous = await c.get("/me")

    assert disabled.status_code == anonymous.status_code == 401
    assert disabled.json() == anonymous.json()


@pytest.mark.asyncio
async def test_clearing_the_flag_restores_the_session(authed_client, session):
    """Reversible: the *session rows* are gone only because the admin route
    deletes them. The predicate itself holds nothing back once cleared."""
    me = authed_client.user  # type: ignore[attr-defined]
    await _disable(session, me)
    assert (await authed_client.get("/me")).status_code == 401

    me.disabled_at = None
    await session.commit()
    assert (await authed_client.get("/me")).status_code == 200


# ---------------------------------------------------------------------------
# §2.3 / AC 3 — login
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_with_the_correct_password_is_generic_401(session, make_user):
    user = await make_user(email="abuser@example.com", password=PASSWORD)
    await _disable(session, user)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        r = await c.post(
            "/auth/login",
            json={"email": "abuser@example.com", "password": PASSWORD},
            headers={"X-Forwarded-For": "203.0.113.9"},
        )
    assert r.status_code == 401
    assert r.json()["detail"] == "invalid_credentials"


@pytest.mark.asyncio
async def test_login_writes_no_login_attempt_row(session, make_user):
    """That ledger answers "is this *account* being guessed at?" and the guess
    was correct — recording it would poison the brute-force signal."""
    user = await make_user(email="abuser@example.com", password=PASSWORD)
    await _disable(session, user)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        await c.post(
            "/auth/login",
            json={"email": "abuser@example.com", "password": PASSWORD},
            headers={"X-Forwarded-For": "203.0.113.9"},
        )

    count = (await session.execute(select(func.count()).select_from(LoginAttempt))).scalar_one()
    assert count == 0


@pytest.mark.asyncio
async def test_login_does_not_clear_the_email_keyed_slate(session, make_user):
    """The refusal lands before `clear_for_email`: a disabled account cannot be
    used as a reset button on a login that never succeeded."""
    user = await make_user(email="abuser@example.com", password=PASSWORD)
    await login_attempt_repo.record(session, email="abuser@example.com", ip=None)
    await login_attempt_repo.record(session, email="abuser@example.com", ip=None)
    await session.commit()
    await _disable(session, user)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        await c.post(
            "/auth/login",
            json={"email": "abuser@example.com", "password": PASSWORD},
            headers={"X-Forwarded-For": "203.0.113.9"},
        )

    remaining = (await session.execute(select(func.count()).select_from(LoginAttempt))).scalar_one()
    assert remaining == 2


@pytest.mark.asyncio
async def test_login_records_an_ip_attempt(session, make_user):
    """With no new code: `InvalidCredentials` propagates into the router's
    existing `except` branch, so an abuser retrying burns their address budget
    like anyone else."""
    user = await make_user(email="abuser@example.com", password=PASSWORD)
    await _disable(session, user)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        await c.post(
            "/auth/login",
            json={"email": "abuser@example.com", "password": PASSWORD},
            headers={"X-Forwarded-For": "203.0.113.9"},
        )

    count = (
        await session.execute(
            select(func.count()).select_from(AuthAttempt).where(AuthAttempt.kind == "login")
        )
    ).scalar_one()
    assert count == 1


@pytest.mark.asyncio
async def test_login_works_again_once_the_flag_is_cleared(session, make_user):
    user = await make_user(email="abuser@example.com", password=PASSWORD)
    await _disable(session, user)
    user.disabled_at = None
    await session.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        r = await c.post(
            "/auth/login",
            json={"email": "abuser@example.com", "password": PASSWORD},
            headers={"X-Forwarded-For": "203.0.113.9"},
        )
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# §3 / AC 6 — the emailed-link paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reset_password_refuses_a_disabled_users_valid_token(session, make_user):
    user = await make_user(email="abuser@example.com", password=PASSWORD)
    issued = await auth_token_service.issue(
        session, user_id=user.id, purpose=auth_token_service.PURPOSE_PASSWORD_RESET
    )
    await session.commit()
    await _disable(session, user)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        r = await c.post(
            "/reset-password",
            json={"token": issued.raw_token, "new_password": "newpassword123"},
        )
    assert r.status_code == 400
    assert r.json()["detail"] == "invalid_token"


@pytest.mark.asyncio
async def test_reset_password_leaves_the_password_alone(session, make_user):
    user = await make_user(email="abuser@example.com", password=PASSWORD)
    before = user.password_hash
    issued = await auth_token_service.issue(
        session, user_id=user.id, purpose=auth_token_service.PURPOSE_PASSWORD_RESET
    )
    await session.commit()
    await _disable(session, user)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        await c.post(
            "/reset-password",
            json={"token": issued.raw_token, "new_password": "newpassword123"},
        )
    await session.refresh(user)
    assert user.password_hash == before


@pytest.mark.asyncio
async def test_verify_email_refuses_a_disabled_users_valid_token(session, make_user):
    """Verification is the flag that makes an account visible in people search —
    precisely what disabling took away."""
    user = await make_user(email="abuser@example.com")
    issued = await auth_token_service.issue(
        session, user_id=user.id, purpose=auth_token_service.PURPOSE_EMAIL_VERIFICATION
    )
    await session.commit()
    await _disable(session, user)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        r = await c.post("/verify-email", json={"token": issued.raw_token})
    assert r.status_code == 400
    assert r.json()["detail"] == "invalid_token"
    await session.refresh(user)
    assert user.email_verified_at is None


@pytest.mark.asyncio
async def test_email_change_confirm_refuses_a_disabled_users_valid_token(session, make_user):
    """The sharpest of the three: left open, a disabled user could detach the
    mailbox that identifies them."""
    user = await make_user(email="abuser@example.com")
    issued = await auth_token_service.issue(
        session,
        user_id=user.id,
        purpose=auth_token_service.PURPOSE_EMAIL_CHANGE,
        payload={"new_email": "elsewhere@example.com"},
    )
    await session.commit()
    await _disable(session, user)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        r = await c.post("/email-change/confirm", json={"token": issued.raw_token})
    assert r.status_code == 400
    assert r.json()["detail"] == "invalid_token"
    await session.refresh(user)
    assert user.email == "abuser@example.com"


@pytest.mark.asyncio
async def test_forgot_password_still_answers_202_and_sends_nothing(
    session, make_user, _stub_outbound_email
):
    """§8.2: the route already returns 202 unconditionally to avoid
    enumeration, so a silent no-op leaks nothing."""
    user = await make_user(email="abuser@example.com")
    await _disable(session, user)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        r = await c.post("/forgot-password", json={"email": "abuser@example.com"})
    assert r.status_code == 202
    assert _stub_outbound_email == []
