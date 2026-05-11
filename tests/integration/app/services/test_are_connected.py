"""Tests for `connection_service.are_connected` (NEU-108)."""

import uuid

import pytest

from tvbf.app.models import User
from tvbf.app.services import connection_service


async def _user(session, email):
    u = User(email=email, password_hash="x", display_name=email.split("@")[0])
    session.add(u)
    await session.flush()
    return u


@pytest.mark.asyncio
async def test_accepted_either_direction_is_true(session):
    a = await _user(session, "a@x.com")
    b = await _user(session, "b@x.com")
    await session.commit()
    req = await connection_service.send_request(session, requester_id=a.id, addressee_id=b.id)
    await connection_service.accept(session, id=req.id, accepting_user_id=b.id)

    assert await connection_service.are_connected(session, a.id, b.id) is True
    assert await connection_service.are_connected(session, b.id, a.id) is True


@pytest.mark.asyncio
async def test_pending_is_false(session):
    a = await _user(session, "a@x.com")
    b = await _user(session, "b@x.com")
    await session.commit()
    await connection_service.send_request(session, requester_id=a.id, addressee_id=b.id)
    assert await connection_service.are_connected(session, a.id, b.id) is False


@pytest.mark.asyncio
async def test_blocked_is_false(session):
    a = await _user(session, "a@x.com")
    b = await _user(session, "b@x.com")
    await session.commit()
    await connection_service.block(session, blocker_id=a.id, blocked_id=b.id)
    assert await connection_service.are_connected(session, a.id, b.id) is False
    assert await connection_service.are_connected(session, b.id, a.id) is False


@pytest.mark.asyncio
async def test_no_relationship_is_false(session):
    a = await _user(session, "a@x.com")
    b = await _user(session, "b@x.com")
    await session.commit()
    assert await connection_service.are_connected(session, a.id, b.id) is False


@pytest.mark.asyncio
async def test_self_is_false(session):
    a = await _user(session, "a@x.com")
    await session.commit()
    assert await connection_service.are_connected(session, a.id, a.id) is False


@pytest.mark.asyncio
async def test_unknown_user_is_false(session):
    a = await _user(session, "a@x.com")
    await session.commit()
    assert await connection_service.are_connected(session, a.id, uuid.uuid4()) is False
