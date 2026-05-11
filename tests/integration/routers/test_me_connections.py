"""Integration tests for /me/connections list + remove."""

from uuid import uuid4

import pytest

from tvbf.app.services import connection_service


async def _accepted_pair(session, requester, addressee):
    req = await connection_service.send_request(
        session, requester_id=requester.id, addressee_id=addressee.id
    )
    await connection_service.accept(session, id=req.id, accepting_user_id=addressee.id)


@pytest.mark.asyncio
async def test_list_returns_other_user_for_either_side(authed_client, make_user, session):
    me = authed_client.user  # type: ignore[attr-defined]
    a = await make_user(email="a@example.com", display_name="Alice")
    b = await make_user(email="b@example.com", display_name="Bob")
    # me is the requester for one, addressee for the other.
    await _accepted_pair(session, me, a)
    await _accepted_pair(session, b, me)

    r = await authed_client.get("/me/connections")
    assert r.status_code == 200
    body = r.json()
    ids = {row["user"]["id"] for row in body}
    assert ids == {str(a.id), str(b.id)}
    # Sorted by display_name → Alice, Bob.
    assert [row["user"]["display_name"] for row in body] == ["Alice", "Bob"]
    # `since` is set.
    assert all(row["since"] is not None for row in body)


@pytest.mark.asyncio
async def test_list_excludes_pending_and_blocked(authed_client, make_user, session):
    me = authed_client.user  # type: ignore[attr-defined]
    pending_user = await make_user(email="p@example.com", display_name="Pending")
    blocked_user = await make_user(email="bl@example.com", display_name="Blocked")
    await connection_service.send_request(session, requester_id=me.id, addressee_id=pending_user.id)
    await connection_service.block(session, blocker_id=me.id, blocked_id=blocked_user.id)

    r = await authed_client.get("/me/connections")
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_remove_either_party(authed_client, make_user, session):
    me = authed_client.user  # type: ignore[attr-defined]
    other = await make_user(email="d@example.com", display_name="Del")
    await _accepted_pair(session, me, other)

    r = await authed_client.delete(f"/me/connections/{other.id}")
    assert r.status_code == 204

    # Row gone from caller's list.
    r2 = await authed_client.get("/me/connections")
    assert r2.json() == []


@pytest.mark.asyncio
async def test_remove_when_no_accepted_returns_404(authed_client, make_user, session):
    me = authed_client.user  # type: ignore[attr-defined]
    other = await make_user(email="ne@example.com", display_name="None")
    # Pending pair only; remove must 404 (only `accepted` rows are removable).
    await connection_service.send_request(session, requester_id=me.id, addressee_id=other.id)
    r = await authed_client.delete(f"/me/connections/{other.id}")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_remove_unknown_user_returns_404(authed_client):
    r = await authed_client.delete(f"/me/connections/{uuid4()}")
    assert r.status_code == 404
