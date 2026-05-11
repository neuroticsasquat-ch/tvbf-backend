"""Integration tests for /connection-requests."""

from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from tvbf.app.services import connection_service
from tvbf.main import app


@pytest.mark.asyncio
async def test_post_requires_auth():
    # Pass CSRF so the request reaches the session check; expect 401 (not 403).
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="https://test",
        headers={"X-CSRF-Token": "x", "Cookie": "csrf_token=x"},
    ) as c:
        r = await c.post(
            "/connection-requests",
            json={"addressee_id": str(uuid4())},
        )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_post_creates_pending(authed_client, make_user):
    other = await make_user(email="other@example.com", display_name="Other")
    r = await authed_client.post("/connection-requests", json={"addressee_id": str(other.id)})
    assert r.status_code == 201
    body = r.json()
    assert body["state"] == "pending"
    assert body["requester"]["id"] == str(authed_client.user.id)  # type: ignore[attr-defined]
    assert body["addressee"]["id"] == str(other.id)
    assert body["responded_at"] is None


@pytest.mark.asyncio
async def test_post_self_returns_400(authed_client):
    me_id = str(authed_client.user.id)  # type: ignore[attr-defined]
    r = await authed_client.post("/connection-requests", json={"addressee_id": me_id})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_post_unknown_addressee_returns_404(authed_client):
    r = await authed_client.post("/connection-requests", json={"addressee_id": str(uuid4())})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_post_duplicate_returns_409(authed_client, make_user):
    other = await make_user(email="dup@example.com", display_name="Dup")
    body = {"addressee_id": str(other.id)}
    r1 = await authed_client.post("/connection-requests", json=body)
    assert r1.status_code == 201
    r2 = await authed_client.post("/connection-requests", json=body)
    assert r2.status_code == 409


@pytest.mark.asyncio
async def test_post_already_accepted_returns_409(authed_client, make_user, session):
    me = authed_client.user  # type: ignore[attr-defined]
    other = await make_user(email="acc@example.com", display_name="Acc")
    req = await connection_service.send_request(session, requester_id=other.id, addressee_id=me.id)
    await connection_service.accept(session, id=req.id, accepting_user_id=me.id)

    r = await authed_client.post("/connection-requests", json={"addressee_id": str(other.id)})
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_post_when_addressee_has_blocked_caller_returns_409(
    authed_client, make_user, session
):
    me = authed_client.user  # type: ignore[attr-defined]
    other = await make_user(email="blkme@example.com", display_name="Blocker")
    await connection_service.block(session, blocker_id=other.id, blocked_id=me.id)

    r = await authed_client.post("/connection-requests", json={"addressee_id": str(other.id)})
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_post_when_caller_has_blocked_addressee_returns_409(
    authed_client, make_user, session
):
    me = authed_client.user  # type: ignore[attr-defined]
    other = await make_user(email="blked@example.com", display_name="Blocked")
    await connection_service.block(session, blocker_id=me.id, blocked_id=other.id)

    r = await authed_client.post("/connection-requests", json={"addressee_id": str(other.id)})
    assert r.status_code == 409
