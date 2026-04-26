"""Integration tests for the login_attempt repo."""

from datetime import UTC, datetime, timedelta

import pytest

from tvbf.app.repos import login_attempt_repo


@pytest.mark.asyncio
async def test_record_inserts_a_row(session):
    await login_attempt_repo.record(session, email="a@b.com", ip="1.2.3.4")
    await session.commit()
    count = await login_attempt_repo.count_since(
        session, email="a@b.com", since=datetime.now(UTC) - timedelta(hours=1)
    )
    assert count == 1


@pytest.mark.asyncio
async def test_count_since_filters_by_window_and_email(session):
    now = datetime.now(UTC)
    # 3 failures for alice, 1 for bob, all "now-ish".
    for _ in range(3):
        await login_attempt_repo.record(session, email="alice@b.com", ip=None)
    await login_attempt_repo.record(session, email="bob@b.com", ip=None)
    await session.commit()

    assert (
        await login_attempt_repo.count_since(
            session, email="alice@b.com", since=now - timedelta(minutes=15)
        )
        == 3
    )
    assert (
        await login_attempt_repo.count_since(
            session, email="bob@b.com", since=now - timedelta(minutes=15)
        )
        == 1
    )
    # Future window — no rows fall inside it.
    assert (
        await login_attempt_repo.count_since(
            session, email="alice@b.com", since=now + timedelta(minutes=1)
        )
        == 0
    )


@pytest.mark.asyncio
async def test_count_since_is_case_insensitive_via_citext(session):
    """email is CITEXT, so 'Alice@B.com' and 'alice@b.com' collide."""
    await login_attempt_repo.record(session, email="Alice@B.com", ip=None)
    await session.commit()
    count = await login_attempt_repo.count_since(
        session, email="alice@b.com", since=datetime.now(UTC) - timedelta(hours=1)
    )
    assert count == 1


@pytest.mark.asyncio
async def test_clear_for_email_only_clears_that_email(session):
    await login_attempt_repo.record(session, email="alice@b.com", ip=None)
    await login_attempt_repo.record(session, email="bob@b.com", ip=None)
    await session.commit()

    await login_attempt_repo.clear_for_email(session, email="alice@b.com")
    await session.commit()

    since = datetime.now(UTC) - timedelta(hours=1)
    assert await login_attempt_repo.count_since(session, email="alice@b.com", since=since) == 0
    assert await login_attempt_repo.count_since(session, email="bob@b.com", since=since) == 1
