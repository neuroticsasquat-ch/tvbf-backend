import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError

from tests.fixtures.handles import new_handle
from tvbf.app.models import Connection, User


@pytest.mark.asyncio
async def test_unordered_pair_unique(session):
    a = User(email="a@example.com", password_hash="x", display_name="A", handle=new_handle())
    b = User(email="b@example.com", password_hash="x", display_name="B", handle=new_handle())
    session.add_all([a, b])
    await session.flush()

    session.add(Connection(requester_id=a.id, addressee_id=b.id, state="pending"))
    await session.commit()

    # Reverse order for the same pair must violate the unordered-pair unique index.
    session.add(Connection(requester_id=b.id, addressee_id=a.id, state="pending"))
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


@pytest.mark.asyncio
async def test_self_connection_rejected(session):
    u = User(email="self@example.com", password_hash="x", display_name="S", handle=new_handle())
    session.add(u)
    await session.flush()

    session.add(Connection(requester_id=u.id, addressee_id=u.id, state="pending"))
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


@pytest.mark.asyncio
async def test_state_enum_roundtrip(session):
    a = User(email="r1@example.com", password_hash="x", display_name="R1", handle=new_handle())
    b = User(email="r2@example.com", password_hash="x", display_name="R2", handle=new_handle())
    c = User(email="r3@example.com", password_hash="x", display_name="R3", handle=new_handle())
    d = User(email="r4@example.com", password_hash="x", display_name="R4", handle=new_handle())
    e = User(email="r5@example.com", password_hash="x", display_name="R5", handle=new_handle())
    f = User(email="r6@example.com", password_hash="x", display_name="R6", handle=new_handle())
    session.add_all([a, b, c, d, e, f])
    await session.flush()

    session.add_all(
        [
            Connection(requester_id=a.id, addressee_id=b.id, state="pending"),
            Connection(requester_id=c.id, addressee_id=d.id, state="accepted"),
            Connection(requester_id=e.id, addressee_id=f.id, state="blocked"),
        ]
    )
    await session.commit()

    rows = (
        (await session.execute(select(Connection.state).order_by(Connection.created_at)))
        .scalars()
        .all()
    )
    assert set(rows) == {"pending", "accepted", "blocked"}


@pytest.mark.asyncio
async def test_invalid_state_rejected(session):
    a = User(email="bad1@example.com", password_hash="x", display_name="B1", handle=new_handle())
    b = User(email="bad2@example.com", password_hash="x", display_name="B2", handle=new_handle())
    session.add_all([a, b])
    await session.flush()

    session.add(Connection(requester_id=a.id, addressee_id=b.id, state="bogus"))
    with pytest.raises(DBAPIError):
        await session.commit()
    await session.rollback()
