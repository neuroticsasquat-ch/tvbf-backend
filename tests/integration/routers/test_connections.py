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


# ---------------------------------------------------------------------------
# Verification gate on outreach (NEU-1161 §2, §3.3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_requires_a_verified_email(unverified_client, make_user):
    other = await make_user(email="gated@example.com", display_name="Gated", verified=True)
    r = await unverified_client.post("/connection-requests", json={"addressee_id": str(other.id)})
    assert r.status_code == 403
    assert r.json()["detail"] == "email_not_verified"


@pytest.mark.asyncio
async def test_post_succeeds_when_the_addressee_is_unverified(authed_client, make_user):
    """The requester is gated; the addressee is never consulted (§3.3)."""
    other = await make_user(email="unverified-addressee@example.com", display_name="Addressee")
    assert other.email_verified_at is None
    r = await authed_client.post("/connection-requests", json={"addressee_id": str(other.id)})
    assert r.status_code == 201
    assert r.json()["state"] == "pending"


@pytest.mark.asyncio
async def test_unverified_caller_can_accept_a_request(unverified_client, make_user, session):
    """Accepting is not outreach — NEU-1152's deliberate asymmetry (§3.3)."""
    me = unverified_client.user  # type: ignore[attr-defined]
    requester = await make_user(email="verified-req@example.com", display_name="Req", verified=True)
    incoming = await connection_service.send_request(
        session, requester_id=requester.id, addressee_id=me.id
    )
    await session.commit()

    r = await unverified_client.post(f"/connection-requests/{incoming.id}/accept")
    assert r.status_code == 200
    assert r.json()["state"] == "accepted"


@pytest.mark.asyncio
async def test_unverified_caller_can_block(unverified_client, make_user):
    """Defensive: an unverified user must always be able to protect themselves."""
    blockee = await make_user(email="blockee@example.com", display_name="Blockee", verified=True)
    r = await unverified_client.post(f"/me/blocks/{blockee.id}")
    assert r.status_code == 201


@pytest.mark.asyncio
async def test_unverified_caller_can_decline_and_disconnect(unverified_client, make_user, session):
    """Withdrawal is not outreach either — declining an incoming request and
    dropping an existing connection both stay open."""
    me = unverified_client.user  # type: ignore[attr-defined]
    decliner = await make_user(email="decliner@example.com", display_name="Decliner", verified=True)
    friend = await make_user(email="exfriend@example.com", display_name="ExFriend", verified=True)
    pending = await connection_service.send_request(
        session, requester_id=decliner.id, addressee_id=me.id
    )
    accepted = await connection_service.send_request(
        session, requester_id=friend.id, addressee_id=me.id
    )
    await connection_service.accept(session, id=accepted.id, accepting_user_id=me.id)
    await session.commit()

    r = await unverified_client.delete(f"/connection-requests/{pending.id}")
    assert r.status_code == 204

    r = await unverified_client.delete(f"/me/connections/{friend.id}")
    assert r.status_code == 204
