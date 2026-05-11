"""Integration tests for /me/blocks (block, unblock, list) plus cross-cutting checks."""

from uuid import uuid4

import pytest

from tvbf.app.repos import connection_repo
from tvbf.app.services import connection_service


@pytest.mark.asyncio
async def test_block_no_prior_connection(authed_client, make_user, session):
    me = authed_client.user  # type: ignore[attr-defined]
    other = await make_user(email="b1@example.com", display_name="B1")

    r = await authed_client.post(f"/me/blocks/{other.id}")
    assert r.status_code == 201
    body = r.json()
    assert body["user"]["id"] == str(other.id)

    pair = await connection_repo.find_pair(session, me.id, other.id)
    assert pair is not None and pair.state == "blocked"


@pytest.mark.asyncio
async def test_block_replaces_accepted_and_filters_lists(authed_client, make_user, session):
    me = authed_client.user  # type: ignore[attr-defined]
    other = await make_user(email="b2@example.com", display_name="B2")
    req = await connection_service.send_request(session, requester_id=me.id, addressee_id=other.id)
    await connection_service.accept(session, id=req.id, accepting_user_id=other.id)

    r = await authed_client.post(f"/me/blocks/{other.id}")
    assert r.status_code == 201

    # Connections list no longer shows the blocked user.
    r2 = await authed_client.get("/me/connections")
    assert all(row["user"]["id"] != str(other.id) for row in r2.json())


@pytest.mark.asyncio
async def test_block_replaces_pending_request(authed_client, make_user, session):
    me = authed_client.user  # type: ignore[attr-defined]
    other = await make_user(email="b3@example.com", display_name="B3")
    # Pending request from the other side.
    await connection_service.send_request(session, requester_id=other.id, addressee_id=me.id)

    r = await authed_client.post(f"/me/blocks/{other.id}")
    assert r.status_code == 201

    # Pending list is now empty.
    r2 = await authed_client.get("/me/connection-requests")
    body = r2.json()
    assert body["incoming"] == [] and body["outgoing"] == []


@pytest.mark.asyncio
async def test_block_self_returns_400(authed_client):
    me_id = str(authed_client.user.id)  # type: ignore[attr-defined]
    r = await authed_client.post(f"/me/blocks/{me_id}")
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_block_unknown_user_returns_404(authed_client):
    r = await authed_client.post(f"/me/blocks/{uuid4()}")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_unblock_by_blocker(authed_client, make_user, session):
    me = authed_client.user  # type: ignore[attr-defined]
    other = await make_user(email="b4@example.com", display_name="B4")
    await connection_service.block(session, blocker_id=me.id, blocked_id=other.id)

    r = await authed_client.delete(f"/me/blocks/{other.id}")
    assert r.status_code == 204

    pair = await connection_repo.find_pair(session, me.id, other.id)
    assert pair is None


@pytest.mark.asyncio
async def test_unblock_by_non_blocker_returns_403(authed_client, make_user, session):
    me = authed_client.user  # type: ignore[attr-defined]
    other = await make_user(email="b5@example.com", display_name="B5")
    # Other user blocks me; I am the blocked party, not the blocker.
    await connection_service.block(session, blocker_id=other.id, blocked_id=me.id)

    r = await authed_client.delete(f"/me/blocks/{other.id}")
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_unblock_no_block_returns_404(authed_client, make_user):
    other = await make_user(email="b6@example.com", display_name="B6")
    r = await authed_client.delete(f"/me/blocks/{other.id}")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_blocks_list_only_shows_caller_blocks(authed_client, make_user, session):
    me = authed_client.user  # type: ignore[attr-defined]
    by_me = await make_user(email="bm@example.com", display_name="ByMe")
    blocks_me = await make_user(email="bm2@example.com", display_name="BlocksMe")

    await connection_service.block(session, blocker_id=me.id, blocked_id=by_me.id)
    # Other side blocks me — must NOT appear in my /me/blocks list.
    await connection_service.block(session, blocker_id=blocks_me.id, blocked_id=me.id)

    r = await authed_client.get("/me/blocks")
    assert r.status_code == 200
    body = r.json()
    ids = {row["user"]["id"] for row in body}
    assert ids == {str(by_me.id)}
    assert all(row["blocked_at"] is not None for row in body)


@pytest.mark.asyncio
async def test_unblock_allows_reconnection(authed_client, make_user, session):
    me = authed_client.user  # type: ignore[attr-defined]
    other = await make_user(email="reconn@example.com", display_name="Reconn")

    await connection_service.block(session, blocker_id=me.id, blocked_id=other.id)
    r = await authed_client.delete(f"/me/blocks/{other.id}")
    assert r.status_code == 204

    # After unblocking, send-request must succeed again.
    r2 = await authed_client.post("/connection-requests", json={"addressee_id": str(other.id)})
    assert r2.status_code == 201


@pytest.mark.asyncio
async def test_search_excludes_blocked_user(authed_client, make_user, session):
    me = authed_client.user  # type: ignore[attr-defined]
    target = await make_user(email="hide@example.com", display_name="HideTarget")
    await connection_service.block(session, blocker_id=me.id, blocked_id=target.id)

    r = await authed_client.get("/users/search", params={"q": "HideTarget"})
    assert r.status_code == 200
    assert all(row["id"] != str(target.id) for row in r.json())
