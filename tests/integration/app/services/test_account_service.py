"""Direct integration tests for account_service.

These call the service with the `session` fixture (real DB, no HTTP layer).
They exercise the same code paths as test_auth_routes.py but with finer
assertions on side effects, and they let coverage actually trace the service
bodies (the ASGITransport-routed tests don't, due to a pytest-cov quirk).
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from tvbf.app.errors import EmailInUse, InvalidCredentials
from tvbf.app.models import Session, User
from tvbf.app.passwords import verify_password
from tvbf.app.repos import session_repo
from tvbf.app.services import account_service

# ---------------------------------------------------------------------------
# signup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_signup_creates_user_session_and_csrf(session):
    user, sess_id, csrf = await account_service.signup(
        session,
        email="alice@example.com",
        password="hunter2hunter2",
        display_name="Alice",
        ttl_days=30,
        user_agent="ua",
        ip="1.2.3.4",
    )
    assert user.email == "alice@example.com"
    assert verify_password("hunter2hunter2", user.password_hash)
    assert sess_id and len(sess_id) >= 32
    assert csrf and csrf != sess_id

    # session row exists with the correct user, ua, ip
    sess = await session_repo.get_active(session, sess_id)
    assert sess is not None
    assert sess.user_id == user.id
    assert sess.user_agent == "ua"
    assert str(sess.ip) == "1.2.3.4"


@pytest.mark.asyncio
async def test_signup_raises_email_in_use_for_duplicate(session):
    await account_service.signup(
        session,
        email="bob@example.com",
        password="hunter2hunter2",
        display_name="Bob",
        ttl_days=30,
        user_agent=None,
        ip=None,
    )
    with pytest.raises(EmailInUse):
        await account_service.signup(
            session,
            email="bob@example.com",
            password="anotherpassword",
            display_name="Bob2",
            ttl_days=30,
            user_agent=None,
            ip=None,
        )


@pytest.mark.asyncio
async def test_signup_email_match_is_case_insensitive(session):
    """citext column → BOB@... collides with bob@..."""
    await account_service.signup(
        session,
        email="case@example.com",
        password="hunter2hunter2",
        display_name="Case",
        ttl_days=30,
        user_agent=None,
        ip=None,
    )
    with pytest.raises(EmailInUse):
        await account_service.signup(
            session,
            email="CASE@example.com",
            password="hunter2hunter2",
            display_name="Case2",
            ttl_days=30,
            user_agent=None,
            ip=None,
        )


# ---------------------------------------------------------------------------
# authenticate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_authenticate_success_returns_new_session(session, make_user):
    await make_user(email="auth@example.com", password="hunter2hunter2")
    user, sess_id, csrf = await account_service.authenticate(
        session,
        email="auth@example.com",
        password="hunter2hunter2",
        ttl_days=30,
        user_agent=None,
        ip=None,
    )
    assert user.email == "auth@example.com"
    assert sess_id
    assert csrf
    sess = await session_repo.get_active(session, sess_id)
    assert sess is not None


@pytest.mark.asyncio
async def test_authenticate_wrong_password_raises(session, make_user):
    await make_user(email="badpw@example.com", password="hunter2hunter2")
    with pytest.raises(InvalidCredentials):
        await account_service.authenticate(
            session,
            email="badpw@example.com",
            password="wrong-password",
            ttl_days=30,
            user_agent=None,
            ip=None,
        )


@pytest.mark.asyncio
async def test_authenticate_unknown_email_raises(session):
    with pytest.raises(InvalidCredentials):
        await account_service.authenticate(
            session,
            email="ghost@example.com",
            password="hunter2hunter2",
            ttl_days=30,
            user_agent=None,
            ip=None,
        )


# ---------------------------------------------------------------------------
# brute-force lockout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_authenticate_locks_out_after_threshold_failures(session, make_user):
    await make_user(email="brute@example.com", password="hunter2hunter2")

    for _ in range(3):
        with pytest.raises(InvalidCredentials):
            await account_service.authenticate(
                session,
                email="brute@example.com",
                password="wrong",
                ttl_days=30,
                user_agent=None,
                ip="1.2.3.4",
                lockout_threshold=3,
                lockout_window_minutes=15,
            )

    # 4th attempt — even with the CORRECT password — must fail because the
    # account is locked.
    with pytest.raises(InvalidCredentials):
        await account_service.authenticate(
            session,
            email="brute@example.com",
            password="hunter2hunter2",
            ttl_days=30,
            user_agent=None,
            ip="1.2.3.4",
            lockout_threshold=3,
            lockout_window_minutes=15,
        )


@pytest.mark.asyncio
async def test_authenticate_records_failure_for_unknown_email(session):
    """Recording a failure even for non-existent emails prevents account
    enumeration: an attacker can't tell from response timing whether the
    account exists. Also limits brute-forcing 'is this a member?'."""
    from datetime import UTC, datetime, timedelta

    from tvbf.app.repos import login_attempt_repo

    with pytest.raises(InvalidCredentials):
        await account_service.authenticate(
            session,
            email="nobody@example.com",
            password="anything",
            ttl_days=30,
            user_agent=None,
            ip=None,
        )
    count = await login_attempt_repo.count_since(
        session,
        email="nobody@example.com",
        since=datetime.now(UTC) - timedelta(hours=1),
    )
    assert count == 1


@pytest.mark.asyncio
async def test_successful_login_clears_failure_counter(session, make_user):
    """A correct password resets the failure count so subsequent typos don't
    accumulate from before the success."""
    from datetime import UTC, datetime, timedelta

    from tvbf.app.repos import login_attempt_repo

    await make_user(email="reset@example.com", password="hunter2hunter2")
    # Two failures.
    for _ in range(2):
        with pytest.raises(InvalidCredentials):
            await account_service.authenticate(
                session,
                email="reset@example.com",
                password="wrong",
                ttl_days=30,
                user_agent=None,
                ip=None,
            )

    # Successful login.
    await account_service.authenticate(
        session,
        email="reset@example.com",
        password="hunter2hunter2",
        ttl_days=30,
        user_agent=None,
        ip=None,
    )

    count = await login_attempt_repo.count_since(
        session,
        email="reset@example.com",
        since=datetime.now(UTC) - timedelta(hours=1),
    )
    assert count == 0


@pytest.mark.asyncio
async def test_lockout_only_counts_failures_inside_window(session, make_user):
    """Old failures outside the window don't contribute to the lockout. We
    invoke authenticate with a tiny window so the previous test's failures
    don't matter — but we also cement the contract."""
    from datetime import UTC, datetime, timedelta

    from tvbf.app.models import LoginAttempt

    user = await make_user(email="window@example.com", password="hunter2hunter2")
    # Insert an old failure (1 hour ago).
    session.add(
        LoginAttempt(
            email="window@example.com",
            attempted_at=datetime.now(UTC) - timedelta(hours=1),
        )
    )
    await session.commit()

    # With a 15-minute window and threshold=1, the old failure is outside the
    # window — login should succeed.
    out_user, sess_id, _ = await account_service.authenticate(
        session,
        email="window@example.com",
        password="hunter2hunter2",
        ttl_days=30,
        user_agent=None,
        ip=None,
        lockout_threshold=1,
        lockout_window_minutes=15,
    )
    assert out_user.id == user.id
    assert sess_id


# ---------------------------------------------------------------------------
# logout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_logout_deletes_session_row(session, make_user):
    user = await make_user(email="lo@example.com")
    _, sess_id, _ = await account_service.authenticate(
        session,
        email="lo@example.com",
        password="hunter2hunter2",
        ttl_days=30,
        user_agent=None,
        ip=None,
    )
    assert await session_repo.get_active(session, sess_id) is not None

    await account_service.logout(session, session_id=sess_id)
    assert await session_repo.get_active(session, sess_id) is None
    # Other sessions for the user should be untouched (none exist here).
    rows = (
        (await session.execute(select(Session).where(Session.user_id == user.id))).scalars().all()
    )
    assert rows == []


@pytest.mark.asyncio
async def test_logout_is_noop_for_unknown_session(session):
    # No session with this id exists. Should not raise.
    await account_service.logout(session, session_id="does-not-exist")


# ---------------------------------------------------------------------------
# change_password
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_change_password_invalidates_old_sessions_and_rotates(session, make_user):
    user = await make_user(email="cp@example.com", password="hunter2hunter2")
    # Open two sessions for this user.
    _, sess_a, _ = await account_service.authenticate(
        session,
        email="cp@example.com",
        password="hunter2hunter2",
        ttl_days=30,
        user_agent=None,
        ip=None,
    )
    _, sess_b, _ = await account_service.authenticate(
        session,
        email="cp@example.com",
        password="hunter2hunter2",
        ttl_days=30,
        user_agent=None,
        ip=None,
    )
    assert sess_a != sess_b

    new_sess, new_csrf = await account_service.change_password(
        session,
        user=user,
        current_password="hunter2hunter2",
        new_password="newpassword99",
        ttl_days=30,
        user_agent=None,
        ip=None,
    )

    # Both old sessions invalidated; the freshly-issued one is active.
    assert await session_repo.get_active(session, sess_a) is None
    assert await session_repo.get_active(session, sess_b) is None
    assert await session_repo.get_active(session, new_sess) is not None
    assert new_csrf

    # New password works.
    await session.refresh(user)
    assert verify_password("newpassword99", user.password_hash)
    assert not verify_password("hunter2hunter2", user.password_hash)


@pytest.mark.asyncio
async def test_change_password_wrong_current_raises_and_does_not_mutate(session, make_user):
    user = await make_user(email="cpwrong@example.com", password="hunter2hunter2")
    original_hash = user.password_hash

    with pytest.raises(InvalidCredentials):
        await account_service.change_password(
            session,
            user=user,
            current_password="wrong",
            new_password="newpassword99",
            ttl_days=30,
            user_agent=None,
            ip=None,
        )

    await session.refresh(user)
    assert user.password_hash == original_hash


# ---------------------------------------------------------------------------
# delete_account
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_account_removes_user(session, make_user):
    user = await make_user(email="del@example.com", password="hunter2hunter2")
    user_id = user.id

    await account_service.delete_account(session, user=user, password="hunter2hunter2")

    found = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    assert found is None


@pytest.mark.asyncio
async def test_delete_account_wrong_password_raises_and_keeps_user(session, make_user):
    user = await make_user(email="delwrong@example.com", password="hunter2hunter2")
    with pytest.raises(InvalidCredentials):
        await account_service.delete_account(session, user=user, password="wrong")

    found = (await session.execute(select(User).where(User.id == user.id))).scalar_one_or_none()
    assert found is not None


# ---------------------------------------------------------------------------
# resolve_session_user
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_session_user_returns_none_for_missing_cookie(session):
    assert await account_service.resolve_session_user(session, session_id=None) is None
    assert await account_service.resolve_session_user(session, session_id="") is None


@pytest.mark.asyncio
async def test_resolve_session_user_returns_none_for_unknown_session(session):
    assert await account_service.resolve_session_user(session, session_id="does-not-exist") is None


@pytest.mark.asyncio
async def test_resolve_session_user_returns_user_and_touches_session(session, make_user):
    user = await make_user(email="rs@example.com")
    _, sess_id, _ = await account_service.authenticate(
        session,
        email="rs@example.com",
        password="hunter2hunter2",
        ttl_days=30,
        user_agent=None,
        ip=None,
    )

    before = (
        (await session.execute(select(Session).where(Session.id == sess_id)))
        .scalar_one()
        .last_seen_at
    )

    resolved = await account_service.resolve_session_user(session, session_id=sess_id)
    assert resolved is not None
    assert resolved.id == user.id

    after = (
        (
            await session.execute(
                select(Session)
                .where(Session.id == sess_id)
                .execution_options(populate_existing=True)
            )
        )
        .scalar_one()
        .last_seen_at
    )
    assert after >= before


@pytest.mark.asyncio
async def test_resolve_session_user_returns_none_for_expired_session(session, make_user):
    """An expired session row exists but session_repo.get_active filters it out."""
    user = await make_user(email="exp@example.com")
    sess_id = "expired-session-id"
    expired = Session(
        id=sess_id,
        user_id=user.id,
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    session.add(expired)
    await session.commit()

    assert await account_service.resolve_session_user(session, session_id=sess_id) is None


@pytest.mark.asyncio
async def test_resolve_session_user_returns_none_when_user_was_deleted(session, make_user):
    """Defensive: session row can outlive its user briefly during cascade. The
    service degrades to None rather than crashing."""
    user = await make_user(email="ghost@example.com")
    _, sess_id, _ = await account_service.authenticate(
        session,
        email="ghost@example.com",
        password="hunter2hunter2",
        ttl_days=30,
        user_agent=None,
        ip=None,
    )
    # ON DELETE CASCADE deletes the session along with the user, so we can't
    # easily recreate this in production. Force the orphan state:
    from tvbf.app.repos import user_repo

    await user_repo.delete_user(session, user.id)
    await session.commit()

    # Session row is gone (cascade); the service returns None either way.
    assert await account_service.resolve_session_user(session, session_id=sess_id) is None
