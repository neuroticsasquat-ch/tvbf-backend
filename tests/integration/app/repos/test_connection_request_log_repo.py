"""The ledger's three queries, exercised directly (NEU-1157 §9, "Test surface").

The service tests reach `reputation_counts` through `current_ceiling`, which
answers a `Throttle` and therefore cannot distinguish "the count was wrong" from
"the rule read it wrongly". These call each query on its own.
"""

from datetime import UTC, datetime, timedelta

import pytest

from tvbf.app.models import ConnectionRequestLog, User
from tvbf.app.repos import connection_request_log_repo as ledger


async def _user(session, email):
    u = User(email=email, password_hash="x", display_name=email.split("@")[0])
    session.add(u)
    await session.flush()
    return u


async def _log(session, requester, addressee, *, outcome, age_days=0, resolved_days_ago=None):
    now = datetime.now(UTC)
    row = ConnectionRequestLog(
        requester_id=requester.id,
        addressee_id=addressee.id,
        outcome=outcome,
        created_at=now - timedelta(days=age_days),
        resolved_at=(
            None if resolved_days_ago is None else now - timedelta(days=resolved_days_ago)
        ),
    )
    session.add(row)
    await session.flush()
    return row


@pytest.mark.asyncio
async def test_record_writes_a_pending_row_with_its_timestamps_populated(session):
    a = await _user(session, "a@x.com")
    b = await _user(session, "b@x.com")
    row = await ledger.record(session, requester_id=a.id, addressee_id=b.id)

    assert row.outcome == ledger.PENDING
    assert row.created_at is not None
    assert row.resolved_at is None


@pytest.mark.asyncio
async def test_count_created_since_counts_every_outcome(session):
    """The count is of rows *created*, which is what makes cancelling worthless
    as a reset (§3.1)."""
    a = await _user(session, "a@x.com")
    for outcome in (
        ledger.PENDING,
        ledger.ACCEPTED,
        ledger.DECLINED,
        ledger.CANCELLED,
        ledger.BLOCKED,
    ):
        await _log(session, a, await _user(session, f"{outcome}@x.com"), outcome=outcome)

    since = datetime.now(UTC) - timedelta(minutes=1440)
    assert await ledger.count_created_since(session, requester_id=a.id, since=since) == 5


@pytest.mark.asyncio
async def test_count_created_since_is_bounded_and_per_requester(session):
    a = await _user(session, "a@x.com")
    b = await _user(session, "b@x.com")
    target = await _user(session, "t@x.com")
    await _log(session, a, target, outcome=ledger.PENDING)
    await _log(session, a, target, outcome=ledger.CANCELLED, age_days=2)
    await _log(session, b, target, outcome=ledger.PENDING)

    since = datetime.now(UTC) - timedelta(minutes=1440)
    assert await ledger.count_created_since(session, requester_id=a.id, since=since) == 1


@pytest.mark.asyncio
async def test_resolve_moves_only_the_pending_row_for_that_ordered_pair(session):
    a = await _user(session, "a@x.com")
    b = await _user(session, "b@x.com")
    settled = await _log(session, a, b, outcome=ledger.ACCEPTED, resolved_days_ago=5)
    live = await _log(session, a, b, outcome=ledger.PENDING)
    reversed_pair = await _log(session, b, a, outcome=ledger.PENDING)

    now = datetime.now(UTC)
    await ledger.resolve(
        session, requester_id=a.id, addressee_id=b.id, outcome=ledger.DECLINED, resolved_at=now
    )
    await session.flush()
    for row in (settled, live, reversed_pair):
        await session.refresh(row)

    assert live.outcome == ledger.DECLINED
    assert live.resolved_at is not None
    assert settled.outcome == ledger.ACCEPTED
    assert reversed_pair.outcome == ledger.PENDING


@pytest.mark.asyncio
async def test_resolve_no_ops_when_the_pair_has_no_pending_row(session):
    """The pre-migration case, a cascaded-away row, and the common `block` —
    all three reach here and must not raise (§2.4)."""
    a = await _user(session, "a@x.com")
    b = await _user(session, "b@x.com")
    await ledger.resolve(
        session,
        requester_id=a.id,
        addressee_id=b.id,
        outcome=ledger.BLOCKED,
        resolved_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_has_decline_since_is_scoped_to_declines_on_the_ordered_pair(session):
    a = await _user(session, "a@x.com")
    b = await _user(session, "b@x.com")
    since = datetime.now(UTC) - timedelta(days=30)

    await _log(session, a, b, outcome=ledger.CANCELLED, resolved_days_ago=1)
    await _log(session, a, b, outcome=ledger.BLOCKED, resolved_days_ago=1)
    assert not await ledger.has_decline_since(
        session, requester_id=a.id, addressee_id=b.id, since=since
    )

    await _log(session, a, b, outcome=ledger.DECLINED, resolved_days_ago=1)
    assert await ledger.has_decline_since(
        session, requester_id=a.id, addressee_id=b.id, since=since
    )
    # Directional: the decliner is not barred from asking.
    assert not await ledger.has_decline_since(
        session, requester_id=b.id, addressee_id=a.id, since=since
    )


@pytest.mark.asyncio
async def test_has_decline_since_reads_resolved_at_not_created_at(session):
    """A request sent long ago and declined yesterday is inside the cooldown; one
    sent yesterday and declined long ago cannot happen, but the query must key on
    the decline either way (§4)."""
    a = await _user(session, "a@x.com")
    b = await _user(session, "b@x.com")
    since = datetime.now(UTC) - timedelta(days=30)
    await _log(session, a, b, outcome=ledger.DECLINED, age_days=90, resolved_days_ago=1)

    assert await ledger.has_decline_since(
        session, requester_id=a.id, addressee_id=b.id, since=since
    )
