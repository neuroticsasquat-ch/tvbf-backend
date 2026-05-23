"""Integration tests for auth_token_service: issue, verify, replay, expiry,
wrong-purpose, and rate-limit behavior."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from tvbf.app.errors import InvalidAuthToken
from tvbf.app.models import AuthToken
from tvbf.app.services import auth_token_service as svc


@pytest.mark.asyncio
async def test_issue_returns_raw_token_and_stores_hashed_row(session, make_user):
    user = await make_user()
    issued = await svc.issue(session, user_id=user.id, purpose=svc.PURPOSE_PASSWORD_RESET)
    await session.commit()

    assert issued.raw_token
    # raw token is not what's stored
    assert issued.token.token_hash != issued.raw_token
    assert len(issued.token.token_hash) == 64  # sha256 hex

    rows = await session.execute(select(AuthToken).where(AuthToken.user_id == user.id))
    row = rows.scalar_one()
    assert row.purpose == svc.PURPOSE_PASSWORD_RESET
    assert row.consumed_at is None
    # expires ~1 hour from now for password reset
    assert timedelta(minutes=55) < row.expires_at - datetime.now(UTC) <= timedelta(hours=1)


@pytest.mark.asyncio
async def test_verify_and_consume_returns_user_and_marks_consumed(session, make_user):
    user = await make_user()
    issued = await svc.issue(session, user_id=user.id, purpose=svc.PURPOSE_EMAIL_VERIFICATION)
    await session.commit()

    verified = await svc.verify_and_consume(
        session,
        raw_token=issued.raw_token,
        purpose=svc.PURPOSE_EMAIL_VERIFICATION,
    )
    await session.commit()
    assert verified.id == user.id

    rows = await session.execute(
        select(AuthToken)
        .where(AuthToken.id == issued.token.id)
        .execution_options(populate_existing=True)
    )
    row = rows.scalar_one()
    assert row.consumed_at is not None


@pytest.mark.asyncio
async def test_replay_after_consume_fails(session, make_user):
    user = await make_user()
    issued = await svc.issue(session, user_id=user.id, purpose=svc.PURPOSE_PASSWORD_RESET)
    await session.commit()

    await svc.verify_and_consume(
        session, raw_token=issued.raw_token, purpose=svc.PURPOSE_PASSWORD_RESET
    )
    await session.commit()

    with pytest.raises(InvalidAuthToken):
        await svc.verify_and_consume(
            session, raw_token=issued.raw_token, purpose=svc.PURPOSE_PASSWORD_RESET
        )


@pytest.mark.asyncio
async def test_unknown_token_raises(session, make_user):
    await make_user()
    with pytest.raises(InvalidAuthToken):
        await svc.verify_and_consume(
            session, raw_token="not-a-real-token", purpose=svc.PURPOSE_PASSWORD_RESET
        )


@pytest.mark.asyncio
async def test_wrong_purpose_raises(session, make_user):
    user = await make_user()
    issued = await svc.issue(session, user_id=user.id, purpose=svc.PURPOSE_EMAIL_VERIFICATION)
    await session.commit()

    with pytest.raises(InvalidAuthToken):
        await svc.verify_and_consume(
            session, raw_token=issued.raw_token, purpose=svc.PURPOSE_PASSWORD_RESET
        )


@pytest.mark.asyncio
async def test_expired_token_raises(session, make_user):
    user = await make_user()
    issued = await svc.issue(session, user_id=user.id, purpose=svc.PURPOSE_PASSWORD_RESET)
    issued.token.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await session.commit()

    with pytest.raises(InvalidAuthToken):
        await svc.verify_and_consume(
            session, raw_token=issued.raw_token, purpose=svc.PURPOSE_PASSWORD_RESET
        )


@pytest.mark.asyncio
async def test_can_issue_blocks_after_one_in_a_minute(session, make_user):
    user = await make_user()
    assert await svc.can_issue(session, user_id=user.id, purpose=svc.PURPOSE_PASSWORD_RESET)
    await svc.issue(session, user_id=user.id, purpose=svc.PURPOSE_PASSWORD_RESET)
    await session.commit()
    assert not await svc.can_issue(session, user_id=user.id, purpose=svc.PURPOSE_PASSWORD_RESET)


@pytest.mark.asyncio
async def test_can_issue_blocks_after_five_in_an_hour(session, make_user):
    user = await make_user()
    # Backdate four tokens to within the hour but outside the per-minute window,
    # then a fifth fresh one — the sixth must be blocked by the hourly cap.
    now = datetime.now(UTC)
    for i in range(4):
        issued = await svc.issue(session, user_id=user.id, purpose=svc.PURPOSE_PASSWORD_RESET)
        issued.token.created_at = now - timedelta(minutes=10 + i)
    await session.commit()

    assert await svc.can_issue(session, user_id=user.id, purpose=svc.PURPOSE_PASSWORD_RESET)
    await svc.issue(session, user_id=user.id, purpose=svc.PURPOSE_PASSWORD_RESET)
    await session.commit()
    assert not await svc.can_issue(session, user_id=user.id, purpose=svc.PURPOSE_PASSWORD_RESET)


@pytest.mark.asyncio
async def test_rate_limit_is_per_purpose(session, make_user):
    user = await make_user()
    await svc.issue(session, user_id=user.id, purpose=svc.PURPOSE_PASSWORD_RESET)
    await session.commit()
    # Different purpose is unaffected.
    assert await svc.can_issue(session, user_id=user.id, purpose=svc.PURPOSE_EMAIL_VERIFICATION)


@pytest.mark.asyncio
async def test_ttls_default_to_spec(session):
    assert svc.ttl_for(svc.PURPOSE_PASSWORD_RESET) == timedelta(hours=1)
    assert svc.ttl_for(svc.PURPOSE_EMAIL_VERIFICATION) == timedelta(hours=24)
