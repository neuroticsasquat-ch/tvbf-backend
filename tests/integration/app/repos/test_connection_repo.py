from datetime import UTC, datetime

import pytest

from tvbf.app.models import User
from tvbf.app.repos import connection_repo


async def _user(session, email):
    u = User(email=email, password_hash="x", display_name=email.split("@")[0])
    session.add(u)
    await session.flush()
    return u


@pytest.mark.asyncio
async def test_find_pair_direction_agnostic(session):
    a = await _user(session, "a@x.com")
    b = await _user(session, "b@x.com")
    await connection_repo.insert(session, requester_id=a.id, addressee_id=b.id, state="pending")
    await session.commit()

    forward = await connection_repo.find_pair(session, a.id, b.id)
    reverse = await connection_repo.find_pair(session, b.id, a.id)
    assert forward is not None
    assert reverse is not None
    assert forward.id == reverse.id


@pytest.mark.asyncio
async def test_find_pair_returns_none_when_absent(session):
    a = await _user(session, "a@x.com")
    b = await _user(session, "b@x.com")
    assert await connection_repo.find_pair(session, a.id, b.id) is None


@pytest.mark.asyncio
async def test_list_accepted_for_user_returns_other_party(session):
    a = await _user(session, "a@x.com")
    b = await _user(session, "b@x.com")
    c = await _user(session, "c@x.com")
    await connection_repo.insert(session, requester_id=a.id, addressee_id=b.id, state="accepted")
    await connection_repo.insert(session, requester_id=c.id, addressee_id=a.id, state="accepted")
    # Pending must not appear.
    d = await _user(session, "d@x.com")
    await connection_repo.insert(session, requester_id=a.id, addressee_id=d.id, state="pending")
    await session.commit()

    rows = await connection_repo.list_accepted_for_user(session, a.id)
    others = {other for _, other in rows}
    assert others == {b.id, c.id}


@pytest.mark.asyncio
async def test_list_pending_splits_incoming_outgoing(session):
    a = await _user(session, "a@x.com")
    b = await _user(session, "b@x.com")
    c = await _user(session, "c@x.com")
    await connection_repo.insert(
        session, requester_id=a.id, addressee_id=b.id, state="pending"
    )  # outgoing for a
    await connection_repo.insert(
        session, requester_id=c.id, addressee_id=a.id, state="pending"
    )  # incoming for a
    await session.commit()

    incoming, outgoing = await connection_repo.list_pending_for_user(session, a.id)
    assert {row.requester_id for row in incoming} == {c.id}
    assert {row.addressee_id for row in outgoing} == {b.id}


@pytest.mark.asyncio
async def test_update_state_sets_responded_at(session):
    a = await _user(session, "a@x.com")
    b = await _user(session, "b@x.com")
    row = await connection_repo.insert(
        session, requester_id=a.id, addressee_id=b.id, state="pending"
    )
    await session.commit()

    when = datetime.now(UTC)
    updated = await connection_repo.update_state(
        session, id=row.id, state="accepted", responded_at=when
    )
    assert updated.state == "accepted"
    assert updated.responded_at is not None


@pytest.mark.asyncio
async def test_delete_removes_row(session):
    a = await _user(session, "a@x.com")
    b = await _user(session, "b@x.com")
    row = await connection_repo.insert(
        session, requester_id=a.id, addressee_id=b.id, state="pending"
    )
    await session.commit()

    await connection_repo.delete(session, row.id)
    await session.commit()
    assert await connection_repo.find_pair(session, a.id, b.id) is None


@pytest.mark.asyncio
async def test_list_blocked_by(session):
    a = await _user(session, "a@x.com")
    b = await _user(session, "b@x.com")
    c = await _user(session, "c@x.com")
    await connection_repo.insert(session, requester_id=a.id, addressee_id=b.id, state="blocked")
    await connection_repo.insert(
        session, requester_id=c.id, addressee_id=a.id, state="blocked"
    )  # a is blocked-by, not blocker — must not appear.
    await session.commit()

    rows = await connection_repo.list_blocked_by(session, a.id)
    assert {row.addressee_id for row in rows} == {b.id}
