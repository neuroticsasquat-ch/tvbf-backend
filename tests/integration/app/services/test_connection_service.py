import pytest

from tvbf.app.errors import (
    ConnectionAlreadyExists,
    ConnectionBlocked,
    ConnectionWrongState,
    NotAConnectionParty,
    NotFound,
    SelfConnectionForbidden,
)
from tvbf.app.models import User
from tvbf.app.repos import connection_repo
from tvbf.app.services import connection_service


async def _user(session, email):
    u = User(email=email, password_hash="x", display_name=email.split("@")[0])
    session.add(u)
    await session.flush()
    return u


@pytest.mark.asyncio
async def test_send_request_self_raises(session):
    a = await _user(session, "a@x.com")
    await session.commit()
    with pytest.raises(SelfConnectionForbidden):
        await connection_service.send_request(session, requester_id=a.id, addressee_id=a.id)


@pytest.mark.asyncio
async def test_send_request_creates_pending(session):
    a = await _user(session, "a@x.com")
    b = await _user(session, "b@x.com")
    await session.commit()
    row = await connection_service.send_request(session, requester_id=a.id, addressee_id=b.id)
    assert row.state == "pending"
    assert row.requester_id == a.id and row.addressee_id == b.id


@pytest.mark.asyncio
async def test_send_request_duplicate_raises_with_existing(session):
    a = await _user(session, "a@x.com")
    b = await _user(session, "b@x.com")
    await session.commit()
    first = await connection_service.send_request(session, requester_id=a.id, addressee_id=b.id)
    with pytest.raises(ConnectionAlreadyExists) as excinfo:
        await connection_service.send_request(session, requester_id=a.id, addressee_id=b.id)
    assert excinfo.value.existing.id == first.id


@pytest.mark.asyncio
async def test_send_request_blocked_raises(session):
    a = await _user(session, "a@x.com")
    b = await _user(session, "b@x.com")
    await session.commit()
    await connection_service.block(session, blocker_id=b.id, blocked_id=a.id)
    with pytest.raises(ConnectionBlocked):
        await connection_service.send_request(session, requester_id=a.id, addressee_id=b.id)


@pytest.mark.asyncio
async def test_accept_by_addressee_transitions(session):
    a = await _user(session, "a@x.com")
    b = await _user(session, "b@x.com")
    await session.commit()
    row = await connection_service.send_request(session, requester_id=a.id, addressee_id=b.id)
    accepted = await connection_service.accept(session, id=row.id, accepting_user_id=b.id)
    assert accepted.state == "accepted"
    assert accepted.responded_at is not None


@pytest.mark.asyncio
async def test_accept_by_non_addressee_raises(session):
    a = await _user(session, "a@x.com")
    b = await _user(session, "b@x.com")
    c = await _user(session, "c@x.com")
    await session.commit()
    row = await connection_service.send_request(session, requester_id=a.id, addressee_id=b.id)
    # Requester cannot accept their own request.
    with pytest.raises(NotAConnectionParty):
        await connection_service.accept(session, id=row.id, accepting_user_id=a.id)
    # Random outsider cannot accept either.
    with pytest.raises(NotAConnectionParty):
        await connection_service.accept(session, id=row.id, accepting_user_id=c.id)


@pytest.mark.asyncio
async def test_accept_already_accepted_raises_wrong_state(session):
    a = await _user(session, "a@x.com")
    b = await _user(session, "b@x.com")
    await session.commit()
    row = await connection_service.send_request(session, requester_id=a.id, addressee_id=b.id)
    await connection_service.accept(session, id=row.id, accepting_user_id=b.id)
    with pytest.raises(ConnectionWrongState):
        await connection_service.accept(session, id=row.id, accepting_user_id=b.id)


@pytest.mark.asyncio
async def test_accept_missing_raises_not_found(session):
    import uuid

    a = await _user(session, "a@x.com")
    await session.commit()
    with pytest.raises(NotFound):
        await connection_service.accept(session, id=uuid.uuid4(), accepting_user_id=a.id)


@pytest.mark.asyncio
async def test_delete_by_requester_or_addressee(session):
    a = await _user(session, "a@x.com")
    b = await _user(session, "b@x.com")
    await session.commit()
    row = await connection_service.send_request(session, requester_id=a.id, addressee_id=b.id)
    await connection_service.delete(session, id=row.id, caller_id=a.id)
    assert await connection_repo.find_pair(session, a.id, b.id) is None


@pytest.mark.asyncio
async def test_delete_by_outsider_raises(session):
    a = await _user(session, "a@x.com")
    b = await _user(session, "b@x.com")
    c = await _user(session, "c@x.com")
    await session.commit()
    row = await connection_service.send_request(session, requester_id=a.id, addressee_id=b.id)
    with pytest.raises(NotAConnectionParty):
        await connection_service.delete(session, id=row.id, caller_id=c.id)


@pytest.mark.asyncio
async def test_remove_connection_requires_accepted(session):
    a = await _user(session, "a@x.com")
    b = await _user(session, "b@x.com")
    await session.commit()
    # No pair → NotFound
    with pytest.raises(NotFound):
        await connection_service.remove_connection(session, user_a=a.id, user_b=b.id)
    # Pending pair → NotFound (only accepted is removable here)
    await connection_service.send_request(session, requester_id=a.id, addressee_id=b.id)
    with pytest.raises(NotFound):
        await connection_service.remove_connection(session, user_a=a.id, user_b=b.id)


@pytest.mark.asyncio
async def test_remove_connection_deletes_accepted(session):
    a = await _user(session, "a@x.com")
    b = await _user(session, "b@x.com")
    await session.commit()
    row = await connection_service.send_request(session, requester_id=a.id, addressee_id=b.id)
    await connection_service.accept(session, id=row.id, accepting_user_id=b.id)
    await connection_service.remove_connection(session, user_a=a.id, user_b=b.id)
    assert await connection_repo.find_pair(session, a.id, b.id) is None


@pytest.mark.asyncio
async def test_block_replaces_existing_connection(session):
    a = await _user(session, "a@x.com")
    b = await _user(session, "b@x.com")
    await session.commit()
    req = await connection_service.send_request(session, requester_id=a.id, addressee_id=b.id)
    await connection_service.accept(session, id=req.id, accepting_user_id=b.id)

    blocked = await connection_service.block(session, blocker_id=a.id, blocked_id=b.id)
    assert blocked.state == "blocked"
    assert blocked.requester_id == a.id and blocked.addressee_id == b.id

    pair = await connection_repo.find_pair(session, a.id, b.id)
    assert pair is not None and pair.state == "blocked"


@pytest.mark.asyncio
async def test_block_self_raises(session):
    a = await _user(session, "a@x.com")
    await session.commit()
    with pytest.raises(SelfConnectionForbidden):
        await connection_service.block(session, blocker_id=a.id, blocked_id=a.id)


@pytest.mark.asyncio
async def test_unblock_requires_blocker(session):
    a = await _user(session, "a@x.com")
    b = await _user(session, "b@x.com")
    await session.commit()
    await connection_service.block(session, blocker_id=a.id, blocked_id=b.id)

    # Blocked user cannot unblock themselves.
    with pytest.raises(NotFound):
        await connection_service.unblock(session, blocker_id=b.id, blocked_id=a.id)

    # Blocker can unblock.
    await connection_service.unblock(session, blocker_id=a.id, blocked_id=b.id)
    assert await connection_repo.find_pair(session, a.id, b.id) is None


@pytest.mark.asyncio
async def test_is_blocked_either_way(session):
    a = await _user(session, "a@x.com")
    b = await _user(session, "b@x.com")
    c = await _user(session, "c@x.com")
    await session.commit()

    assert not await connection_service.is_blocked_either_way(session, user_a=a.id, user_b=b.id)

    await connection_service.block(session, blocker_id=a.id, blocked_id=b.id)
    assert await connection_service.is_blocked_either_way(session, user_a=a.id, user_b=b.id)
    # Symmetric.
    assert await connection_service.is_blocked_either_way(session, user_a=b.id, user_b=a.id)

    # Accepted connections aren't "blocked".
    req = await connection_service.send_request(session, requester_id=a.id, addressee_id=c.id)
    await connection_service.accept(session, id=req.id, accepting_user_id=c.id)
    assert not await connection_service.is_blocked_either_way(session, user_a=a.id, user_b=c.id)
