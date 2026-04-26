"""Integration tests for session_repo helpers.

Extracted from test_app_auth.py — covers the async DB tests that use the
`session` fixture (real Postgres).
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from tvbf.app.models import Session, User
from tvbf.app.repos import session_repo
from tvbf.app.tokens import new_session_id

# ---------------------------------------------------------------------------
# Test helper: wraps the new repo pattern so existing test call sites
# that expect `await _create_session(session, user_id=..., ttl_days=...)` work.
# ---------------------------------------------------------------------------


async def _create_session(db, *, user_id, ttl_days):
    sess_id = new_session_id()
    await session_repo.create(
        db,
        session_id=sess_id,
        user_id=user_id,
        ttl_days=ttl_days,
        user_agent=None,
        ip=None,
    )
    return sess_id


@pytest.mark.asyncio
async def test_create_and_lookup_session(session):
    user = User(email="g@example.com", password_hash="x", display_name="G")
    session.add(user)
    await session.flush()

    sess_id = await _create_session(session, user_id=user.id, ttl_days=7)
    await session.commit()

    found = await session_repo.get_active(session, sess_id)
    assert found is not None
    assert found.user_id == user.id
    assert found.expires_at > datetime.now(UTC)


@pytest.mark.asyncio
async def test_lookup_session_returns_none_when_expired(session):
    user = User(email="h@example.com", password_hash="x", display_name="H")
    session.add(user)
    await session.flush()
    sess = Session(
        id="expired",
        user_id=user.id,
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    session.add(sess)
    await session.commit()
    assert await session_repo.get_active(session, "expired") is None


@pytest.mark.asyncio
async def test_lookup_session_returns_none_for_unknown(session):
    assert await session_repo.get_active(session, "does-not-exist") is None


@pytest.mark.asyncio
async def test_touch_session_updates_last_seen(session):
    user = User(email="i@example.com", password_hash="x", display_name="I")
    session.add(user)
    await session.flush()
    sess_id = await _create_session(session, user_id=user.id, ttl_days=30)
    await session.commit()

    before = (
        (await session.execute(select(Session).where(Session.id == sess_id)))
        .scalar_one()
        .last_seen_at
    )

    await session_repo.touch(session, sess_id)
    await session.commit()

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
async def test_delete_session(session):
    user = User(email="j@example.com", password_hash="x", display_name="J")
    session.add(user)
    await session.flush()
    sess_id = await _create_session(session, user_id=user.id, ttl_days=30)
    await session.commit()

    await session_repo.delete(session, sess_id)
    await session.commit()

    assert await session_repo.get_active(session, sess_id) is None


@pytest.mark.asyncio
async def test_make_user_fixture(make_user):
    u = await make_user(email="fixture@example.com")
    assert u.email == "fixture@example.com"
    assert u.password_hash.startswith("$argon2id$")


@pytest.mark.asyncio
async def test_delete_user_sessions(session):
    user = User(email="k@example.com", password_hash="x", display_name="K")
    session.add(user)
    await session.flush()
    a = await _create_session(session, user_id=user.id, ttl_days=30)
    b = await _create_session(session, user_id=user.id, ttl_days=30)
    await session.commit()

    await session_repo.delete_all_for_user(session, user.id)
    await session.commit()

    assert await session_repo.get_active(session, a) is None
    assert await session_repo.get_active(session, b) is None
