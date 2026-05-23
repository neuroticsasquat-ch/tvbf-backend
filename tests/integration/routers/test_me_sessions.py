"""Integration tests for GET /me/sessions and the debounced touch behavior."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update

from tvbf.app.models import Session
from tvbf.app.repos import session_repo
from tvbf.app.tokens import new_session_id
from tvbf.main import app


@pytest.mark.asyncio
async def test_list_returns_only_my_sessions_marks_current_and_orders_by_last_seen(
    authed_client, make_user, session
):
    me = authed_client.user
    # Seed an older session for me + a session for another user.
    older = new_session_id()
    await session_repo.create(
        session,
        session_id=older,
        user_id=me.id,
        ttl_days=30,
        user_agent="Mozilla/5.0 (Windows NT 10.0) Firefox/124.0",
        ip="10.0.0.1",
    )
    other = await make_user(email="other@example.com")
    await session_repo.create(
        session,
        session_id=new_session_id(),
        user_id=other.id,
        ttl_days=30,
        user_agent="Mozilla/5.0 (Macintosh; Mac OS X 10_15_7) Safari/605",
        ip="10.0.0.2",
    )
    await session.commit()

    # Make the seeded "me" session look older than the authed session.
    await session.execute(
        update(Session)
        .where(Session.id == older)
        .values(last_seen_at=datetime.now(UTC) - timedelta(hours=2))
    )
    await session.commit()

    r = await authed_client.get("/me/sessions")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 2  # only my sessions

    # Most-recently-active first → authed_client's session.
    assert rows[0]["is_current"] is True
    assert rows[1]["is_current"] is False
    assert rows[1]["device_label"] == "Firefox on Windows"
    assert rows[1]["ip"] == "10.0.0.1"
    assert rows[0]["id"] != rows[1]["id"]


@pytest.mark.asyncio
async def test_list_excludes_expired_sessions(authed_client, session):
    me = authed_client.user
    expired = new_session_id()
    await session_repo.create(
        session,
        session_id=expired,
        user_id=me.id,
        ttl_days=30,
        user_agent=None,
        ip=None,
    )
    await session.execute(
        update(Session)
        .where(Session.id == expired)
        .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    await session.commit()

    r = await authed_client.get("/me/sessions")
    assert r.status_code == 200
    ids = [row["id"] for row in r.json()]
    assert expired not in ids


@pytest.mark.asyncio
async def test_list_requires_auth():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        r = await c.get("/me/sessions")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# touch() debouncing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_touch_is_a_noop_within_the_debounce_window(make_user, session):
    user = await make_user()
    sess_id = new_session_id()
    await session_repo.create(
        session,
        session_id=sess_id,
        user_id=user.id,
        ttl_days=30,
        user_agent=None,
        ip=None,
    )
    await session.commit()

    row = (await session.execute(select(Session).where(Session.id == sess_id))).scalar_one()
    original = row.last_seen_at

    # Second touch within the same minute must not bump last_seen_at.
    await session_repo.touch(session, sess_id)
    await session.commit()

    row = (
        await session.execute(
            select(Session).where(Session.id == sess_id).execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert row.last_seen_at == original


@pytest.mark.asyncio
async def test_touch_updates_after_debounce_window(make_user, session):
    user = await make_user()
    sess_id = new_session_id()
    await session_repo.create(
        session,
        session_id=sess_id,
        user_id=user.id,
        ttl_days=30,
        user_agent=None,
        ip=None,
    )
    # Backdate last_seen_at past the debounce threshold.
    await session.execute(
        update(Session)
        .where(Session.id == sess_id)
        .values(last_seen_at=datetime.now(UTC) - timedelta(minutes=5))
    )
    await session.commit()

    backdated = (
        (await session.execute(select(Session).where(Session.id == sess_id)))
        .scalar_one()
        .last_seen_at
    )

    await session_repo.touch(session, sess_id)
    await session.commit()

    row = (
        await session.execute(
            select(Session).where(Session.id == sess_id).execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert row.last_seen_at > backdated
