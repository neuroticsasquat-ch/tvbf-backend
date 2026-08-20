"""The per-requester connection budget and its two ceilings (NEU-1157 §3).

The ledger is seeded directly here rather than driven through the service, so a
row's `created_at` and `resolved_at` can be placed anywhere in the window —
ageing a `pending` row past the "ignored" threshold is not otherwise reachable
in a test.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete

from tvbf.app.errors import DeclineCooldownActive, TooManyAttempts
from tvbf.app.models import ConnectionRequestLog, User
from tvbf.app.repos import connection_request_log_repo as ledger
from tvbf.app.services import connection_throttle
from tvbf.config import Settings


async def _user(session, email):
    u = User(email=email, password_hash="x", display_name=email.split("@")[0])
    session.add(u)
    await session.flush()
    return u


async def _log(session, requester, addressee, *, outcome, age_days=0, resolved_days_ago=None):
    now = datetime.now(UTC)
    session.add(
        ConnectionRequestLog(
            requester_id=requester.id,
            addressee_id=addressee.id,
            outcome=outcome,
            created_at=now - timedelta(days=age_days),
            resolved_at=(
                None if resolved_days_ago is None else now - timedelta(days=resolved_days_ago)
            ),
        )
    )
    await session.flush()


def _settings(**overrides) -> Settings:
    return Settings(**overrides)  # type: ignore[call-arg]  # pydantic-settings reads from env


async def _seed(session, requester, *, accepted=0, declined=0, cancelled=0, blocked=0, ignored=0):
    """`ignored` rows are `pending` and old enough to have aged."""
    for i in range(accepted):
        await _log(
            session, requester, await _user(session, f"acc{i}@x.com"), outcome=ledger.ACCEPTED
        )
    for i in range(declined):
        await _log(
            session, requester, await _user(session, f"dec{i}@x.com"), outcome=ledger.DECLINED
        )
    for i in range(cancelled):
        await _log(
            session, requester, await _user(session, f"can{i}@x.com"), outcome=ledger.CANCELLED
        )
    for i in range(blocked):
        await _log(
            session, requester, await _user(session, f"blk{i}@x.com"), outcome=ledger.BLOCKED
        )
    for i in range(ignored):
        await _log(
            session,
            requester,
            await _user(session, f"ign{i}@x.com"),
            outcome=ledger.PENDING,
            age_days=20,
        )


@pytest.mark.asyncio
async def test_fresh_account_gets_the_full_ceiling(session):
    """§3.2's recorded consequence of demotion-not-earn-your-ceiling: an account
    with no history at all runs its first day at MAX."""
    a = await _user(session, "fresh@x.com")
    settings = _settings()
    ceiling = await connection_throttle.current_ceiling(
        session, requester_id=a.id, settings=settings
    )
    assert ceiling == settings.connection_request_throttle


@pytest.mark.asyncio
async def test_below_min_sample_keeps_the_full_ceiling(session):
    """4 adverse out of 4 is a 100% adverse rate and still not a sample."""
    a = await _user(session, "small@x.com")
    await _seed(session, a, declined=4)
    settings = _settings()
    assert (
        await connection_throttle.current_ceiling(session, requester_id=a.id, settings=settings)
        == settings.connection_request_throttle
    )


@pytest.mark.asyncio
async def test_exactly_at_the_threshold_demotes(session):
    """Sample 6, adverse 3 — `adverse * 100 >= sample * 50` holds at equality.
    Integer arithmetic, so the boundary is exact rather than a float rate."""
    a = await _user(session, "edge@x.com")
    await _seed(session, a, accepted=3, declined=3)
    settings = _settings()
    assert (
        await connection_throttle.current_ceiling(session, requester_id=a.id, settings=settings)
        == settings.connection_request_floor_throttle
    )


@pytest.mark.asyncio
async def test_just_under_the_threshold_keeps_the_full_ceiling(session):
    """Sample 7, adverse 3 — 42.8%, under the bar."""
    a = await _user(session, "under@x.com")
    await _seed(session, a, accepted=4, declined=3)
    settings = _settings()
    assert (
        await connection_throttle.current_ceiling(session, requester_id=a.id, settings=settings)
        == settings.connection_request_throttle
    )


@pytest.mark.asyncio
async def test_blocked_counts_as_adverse(session):
    a = await _user(session, "blocked@x.com")
    await _seed(session, a, accepted=2, declined=1, blocked=2)
    settings = _settings()
    assert (
        await connection_throttle.current_ceiling(session, requester_id=a.id, settings=settings)
        == settings.connection_request_floor_throttle
    )


@pytest.mark.asyncio
async def test_cancelled_moves_neither_numerator_nor_denominator(session):
    """20 cancellations beside 5 acceptances leave the account at MAX — if
    `cancelled` sat in the denominator this would be 0% adverse but still a
    sample; if it sat in the numerator it would demote instantly."""
    a = await _user(session, "cancels@x.com")
    await _seed(session, a, accepted=5, cancelled=20)
    settings = _settings()
    assert (
        await connection_throttle.current_ceiling(session, requester_id=a.id, settings=settings)
        == settings.connection_request_throttle
    )
    accepted, adverse = await ledger.reputation_counts(
        session,
        requester_id=a.id,
        since=datetime.now(UTC) - timedelta(days=30),
        ignored_before=datetime.now(UTC) - timedelta(days=14),
    )
    assert (accepted, adverse) == (5, 0)


@pytest.mark.asyncio
async def test_a_pending_row_is_adverse_at_15_days_and_not_at_13(session):
    a = await _user(session, "aging@x.com")
    b = await _user(session, "target@x.com")
    await _log(session, a, b, outcome=ledger.PENDING, age_days=13)
    window = {
        "since": datetime.now(UTC) - timedelta(days=30),
        "ignored_before": datetime.now(UTC) - timedelta(days=14),
    }
    assert await ledger.reputation_counts(session, requester_id=a.id, **window) == (0, 0)

    await _log(session, a, await _user(session, "t2@x.com"), outcome=ledger.PENDING, age_days=15)
    assert await ledger.reputation_counts(session, requester_id=a.id, **window) == (0, 1)


@pytest.mark.asyncio
async def test_rows_outside_the_reputation_window_are_ignored(session):
    """A wholly adverse history that has aged out restores the full ceiling with
    no change in behaviour — §3.2's automatic recovery."""
    a = await _user(session, "recovered@x.com")
    for i in range(6):
        await _log(
            session, a, await _user(session, f"old{i}@x.com"), outcome=ledger.DECLINED, age_days=31
        )
    settings = _settings()
    assert (
        await connection_throttle.current_ceiling(session, requester_id=a.id, settings=settings)
        == settings.connection_request_throttle
    )


@pytest.mark.asyncio
async def test_another_users_history_does_not_count(session):
    a = await _user(session, "clean@x.com")
    b = await _user(session, "dirty@x.com")
    await _seed(session, b, declined=6)
    settings = _settings()
    assert (
        await connection_throttle.current_ceiling(session, requester_id=a.id, settings=settings)
        == settings.connection_request_throttle
    )


@pytest.mark.asyncio
async def test_enforce_raises_at_the_ceiling_with_the_whole_window(session):
    a = await _user(session, "capped@x.com")
    settings = _settings(CONNECTION_REQUEST_THROTTLE_MAX=2)
    for i in range(2):
        await _log(session, a, await _user(session, f"s{i}@x.com"), outcome=ledger.PENDING)

    with pytest.raises(TooManyAttempts) as err:
        await connection_throttle.enforce(session, requester_id=a.id, settings=settings)
    assert err.value.retry_after_seconds == settings.connection_request_throttle_window_minutes * 60


@pytest.mark.asyncio
async def test_enforce_counts_every_outcome_not_just_pending(session):
    """AC 3 at the arithmetic level: the count is of rows *created*."""
    a = await _user(session, "mixed@x.com")
    settings = _settings(CONNECTION_REQUEST_THROTTLE_MAX=3)
    await _log(session, a, await _user(session, "m1@x.com"), outcome=ledger.CANCELLED)
    await _log(session, a, await _user(session, "m2@x.com"), outcome=ledger.ACCEPTED)
    await _log(session, a, await _user(session, "m3@x.com"), outcome=ledger.DECLINED)
    with pytest.raises(TooManyAttempts):
        await connection_throttle.enforce(session, requester_id=a.id, settings=settings)


@pytest.mark.asyncio
async def test_enforce_uses_the_floor_when_demoted(session):
    """Three requests today is fine at MAX and refused once the reputation rule
    has dropped the ceiling to 2 — the mid-day demotion of §3.3."""
    a = await _user(session, "demoted@x.com")
    settings = _settings()
    for i in range(3):
        await _log(session, a, await _user(session, f"t{i}@x.com"), outcome=ledger.PENDING)
    await connection_throttle.enforce(session, requester_id=a.id, settings=settings)

    await _seed(session, a, accepted=2, declined=4)
    with pytest.raises(TooManyAttempts) as err:
        await connection_throttle.enforce(session, requester_id=a.id, settings=settings)
    assert err.value.retry_after_seconds == settings.connection_request_throttle_window_minutes * 60


@pytest.mark.asyncio
async def test_created_outside_the_daily_window_does_not_count(session):
    a = await _user(session, "yesterday@x.com")
    settings = _settings(CONNECTION_REQUEST_THROTTLE_MAX=1)
    await _log(session, a, await _user(session, "old@x.com"), outcome=ledger.PENDING, age_days=2)
    await connection_throttle.enforce(session, requester_id=a.id, settings=settings)


@pytest.mark.asyncio
async def test_decline_cooldown_holds_inside_the_window_and_lapses_outside(session):
    a = await _user(session, "asker@x.com")
    b = await _user(session, "decliner@x.com")
    settings = _settings()

    await _log(session, a, b, outcome=ledger.DECLINED, age_days=29, resolved_days_ago=29)
    with pytest.raises(DeclineCooldownActive):
        await connection_throttle.enforce_decline_cooldown(
            session, requester_id=a.id, addressee_id=b.id, settings=settings
        )

    await session.execute(
        delete(ConnectionRequestLog).where(ConnectionRequestLog.requester_id == a.id)
    )
    await _log(session, a, b, outcome=ledger.DECLINED, age_days=31, resolved_days_ago=31)
    await connection_throttle.enforce_decline_cooldown(
        session, requester_id=a.id, addressee_id=b.id, settings=settings
    )


@pytest.mark.asyncio
async def test_decline_cooldown_is_directional_and_per_pair(session):
    """It bars the person who was declined, not the decliner, and says nothing
    about any other pair."""
    a = await _user(session, "a@x.com")
    b = await _user(session, "b@x.com")
    c = await _user(session, "c@x.com")
    settings = _settings()
    await _log(session, a, b, outcome=ledger.DECLINED, resolved_days_ago=1)

    await connection_throttle.enforce_decline_cooldown(
        session, requester_id=b.id, addressee_id=a.id, settings=settings
    )
    await connection_throttle.enforce_decline_cooldown(
        session, requester_id=a.id, addressee_id=c.id, settings=settings
    )


@pytest.mark.asyncio
async def test_cancelled_and_blocked_do_not_trigger_the_cooldown(session):
    """Your own withdrawal must not lock you out of correcting it, and a block
    is already 409'd by the surviving `app.connection` row."""
    a = await _user(session, "self@x.com")
    b = await _user(session, "other@x.com")
    c = await _user(session, "blocker@x.com")
    settings = _settings()
    await _log(session, a, b, outcome=ledger.CANCELLED, resolved_days_ago=1)
    await _log(session, a, c, outcome=ledger.BLOCKED, resolved_days_ago=1)

    await connection_throttle.enforce_decline_cooldown(
        session, requester_id=a.id, addressee_id=b.id, settings=settings
    )
    await connection_throttle.enforce_decline_cooldown(
        session, requester_id=a.id, addressee_id=c.id, settings=settings
    )
