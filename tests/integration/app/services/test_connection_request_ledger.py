"""The ledger writes that ride the connection lifecycle (NEU-1157 §2.3).

Every transition that exists in the code gets its own stored outcome, and two of
them share one code path — a decline and a cancel are told apart only by
`caller_id`, and only in `delete_pending_request`.
"""

import pytest
from sqlalchemy import delete, select

from tests.fixtures.handles import new_handle
from tvbf.app.models import ConnectionRequestLog, User
from tvbf.app.repos import connection_request_log_repo as ledger
from tvbf.app.services import connection_service


async def _user(session, email):
    u = User(
        email=email,
        password_hash="x",
        display_name=email.split("@")[0],
        handle=new_handle(),
    )
    session.add(u)
    await session.flush()
    return u


async def _rows(session, requester_id=None) -> list[ConnectionRequestLog]:
    stmt = select(ConnectionRequestLog).order_by(ConnectionRequestLog.id)
    if requester_id is not None:
        stmt = stmt.where(ConnectionRequestLog.requester_id == requester_id)
    return list((await session.execute(stmt)).scalars().all())


@pytest.mark.asyncio
async def test_send_request_records_pending(session):
    a = await _user(session, "a@x.com")
    b = await _user(session, "b@x.com")
    await connection_service.send_request(session, requester_id=a.id, addressee_id=b.id)

    (row,) = await _rows(session)
    assert (row.requester_id, row.addressee_id) == (a.id, b.id)
    assert row.outcome == ledger.PENDING
    assert row.resolved_at is None


@pytest.mark.asyncio
async def test_accept_resolves_to_accepted(session):
    a = await _user(session, "a@x.com")
    b = await _user(session, "b@x.com")
    conn = await connection_service.send_request(session, requester_id=a.id, addressee_id=b.id)
    await connection_service.accept(session, id=conn.id, accepting_user_id=b.id)

    (row,) = await _rows(session)
    assert row.outcome == ledger.ACCEPTED
    assert row.resolved_at is not None


@pytest.mark.asyncio
async def test_addressee_deleting_records_declined(session):
    a = await _user(session, "a@x.com")
    b = await _user(session, "b@x.com")
    conn = await connection_service.send_request(session, requester_id=a.id, addressee_id=b.id)
    await connection_service.delete_pending_request(session, id=conn.id, caller_id=b.id)

    (row,) = await _rows(session)
    assert row.outcome == ledger.DECLINED
    assert row.resolved_at is not None


@pytest.mark.asyncio
async def test_requester_deleting_records_cancelled(session):
    a = await _user(session, "a@x.com")
    b = await _user(session, "b@x.com")
    conn = await connection_service.send_request(session, requester_id=a.id, addressee_id=b.id)
    await connection_service.delete_pending_request(session, id=conn.id, caller_id=a.id)

    (row,) = await _rows(session)
    assert row.outcome == ledger.CANCELLED


@pytest.mark.asyncio
async def test_cancel_then_resend_leaves_two_rows(session):
    """AC 3 at the ledger: the cancelled row stays counted, so the slot never
    comes back."""
    a = await _user(session, "a@x.com")
    b = await _user(session, "b@x.com")
    conn = await connection_service.send_request(session, requester_id=a.id, addressee_id=b.id)
    await connection_service.delete_pending_request(session, id=conn.id, caller_id=a.id)
    await connection_service.send_request(session, requester_id=a.id, addressee_id=b.id)

    rows = await _rows(session)
    assert [r.outcome for r in rows] == [ledger.CANCELLED, ledger.PENDING]


@pytest.mark.asyncio
async def test_block_by_the_addressee_records_blocked(session):
    a = await _user(session, "a@x.com")
    b = await _user(session, "b@x.com")
    await connection_service.send_request(session, requester_id=a.id, addressee_id=b.id)
    await connection_service.block(session, blocker_id=b.id, blocked_id=a.id)

    (row,) = await _rows(session)
    assert row.outcome == ledger.BLOCKED
    assert row.resolved_at is not None


@pytest.mark.asyncio
async def test_block_by_the_requester_records_cancelled(session):
    """They withdrew. Charging them an adverse outcome for their own decision to
    disengage would be backwards."""
    a = await _user(session, "a@x.com")
    b = await _user(session, "b@x.com")
    await connection_service.send_request(session, requester_id=a.id, addressee_id=b.id)
    await connection_service.block(session, blocker_id=a.id, blocked_id=b.id)

    (row,) = await _rows(session)
    assert row.outcome == ledger.CANCELLED


@pytest.mark.asyncio
async def test_block_with_no_request_writes_nothing(session):
    """The common case for `block` — a stranger nobody ever requested."""
    a = await _user(session, "a@x.com")
    b = await _user(session, "b@x.com")
    await connection_service.block(session, blocker_id=a.id, blocked_id=b.id)
    assert await _rows(session) == []


@pytest.mark.asyncio
async def test_block_after_acceptance_leaves_the_accepted_row_alone(session):
    """Outcomes are terminal: the request *was* accepted, and a later block does
    not make that untrue."""
    a = await _user(session, "a@x.com")
    b = await _user(session, "b@x.com")
    conn = await connection_service.send_request(session, requester_id=a.id, addressee_id=b.id)
    await connection_service.accept(session, id=conn.id, accepting_user_id=b.id)
    await connection_service.block(session, blocker_id=b.id, blocked_id=a.id)

    (row,) = await _rows(session)
    assert row.outcome == ledger.ACCEPTED


@pytest.mark.asyncio
async def test_unblock_and_remove_connection_write_nothing(session):
    a = await _user(session, "a@x.com")
    b = await _user(session, "b@x.com")
    conn = await connection_service.send_request(session, requester_id=a.id, addressee_id=b.id)
    await connection_service.accept(session, id=conn.id, accepting_user_id=b.id)
    await connection_service.remove_connection(session, user_a=a.id, user_b=b.id)
    await connection_service.block(session, blocker_id=a.id, blocked_id=b.id)
    await connection_service.unblock(session, blocker_id=a.id, blocked_id=b.id)

    (row,) = await _rows(session)
    assert row.outcome == ledger.ACCEPTED


@pytest.mark.asyncio
async def test_accepting_a_request_with_no_ledger_row_succeeds(session):
    """The pre-migration case (AC 9). The row is created behind the service's
    back, exactly as a request predating the ledger has none."""
    a = await _user(session, "a@x.com")
    b = await _user(session, "b@x.com")
    conn = await connection_service.send_request(session, requester_id=a.id, addressee_id=b.id)
    await session.execute(delete(ConnectionRequestLog))
    await session.commit()

    updated = await connection_service.accept(session, id=conn.id, accepting_user_id=b.id)
    assert updated.state == "accepted"
    assert await _rows(session) == []


@pytest.mark.asyncio
async def test_declining_a_request_with_no_ledger_row_succeeds(session):
    a = await _user(session, "a@x.com")
    b = await _user(session, "b@x.com")
    conn = await connection_service.send_request(session, requester_id=a.id, addressee_id=b.id)
    await session.execute(delete(ConnectionRequestLog))
    await session.commit()

    await connection_service.delete_pending_request(session, id=conn.id, caller_id=b.id)
    assert await _rows(session) == []


@pytest.mark.asyncio
async def test_deleting_a_user_cascades_their_ledger_rows(session):
    """Retention is "for as long as both accounts exist" (§2.5) — including when
    it is the *target* who leaves."""
    a = await _user(session, "a@x.com")
    b = await _user(session, "b@x.com")
    await connection_service.send_request(session, requester_id=a.id, addressee_id=b.id)
    await session.execute(delete(User).where(User.id == b.id))
    await session.commit()

    assert await _rows(session) == []
