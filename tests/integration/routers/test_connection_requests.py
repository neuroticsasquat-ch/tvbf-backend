"""Integration tests for /me/connection-requests, accept, and reject/cancel."""

from uuid import uuid4

import pytest

from tvbf.app.services import connection_service


@pytest.mark.asyncio
async def test_list_splits_incoming_and_outgoing(authed_client, make_user, session):
    me = authed_client.user  # type: ignore[attr-defined]
    incoming_user = await make_user(email="in@example.com", display_name="Inbound")
    outgoing_user = await make_user(email="out@example.com", display_name="Outbound")
    other = await make_user(email="acc@example.com", display_name="Accepted")

    incoming = await connection_service.send_request(
        session, requester_id=incoming_user.id, addressee_id=me.id
    )
    outgoing = await connection_service.send_request(
        session, requester_id=me.id, addressee_id=outgoing_user.id
    )
    # Already-accepted pair must NOT show up in pending lists.
    accepted = await connection_service.send_request(
        session, requester_id=me.id, addressee_id=other.id
    )
    await connection_service.accept(session, id=accepted.id, accepting_user_id=other.id)

    r = await authed_client.get("/me/connection-requests")
    assert r.status_code == 200
    body = r.json()

    assert {row["id"] for row in body["incoming"]} == {str(incoming.id)}
    assert {row["id"] for row in body["outgoing"]} == {str(outgoing.id)}

    inc = body["incoming"][0]
    assert inc["requester"]["id"] == str(incoming_user.id)
    assert inc["addressee"]["id"] == str(me.id)
    out = body["outgoing"][0]
    assert out["requester"]["id"] == str(me.id)
    assert out["addressee"]["id"] == str(outgoing_user.id)


@pytest.mark.asyncio
async def test_accept_by_addressee(authed_client, make_user, session):
    me = authed_client.user  # type: ignore[attr-defined]
    requester = await make_user(email="req@example.com", display_name="Req")
    row = await connection_service.send_request(
        session, requester_id=requester.id, addressee_id=me.id
    )

    r = await authed_client.post(f"/connection-requests/{row.id}/accept")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "accepted"
    assert body["responded_at"] is not None


@pytest.mark.asyncio
async def test_accept_by_requester_returns_403(authed_client, make_user, session):
    me = authed_client.user  # type: ignore[attr-defined]
    other = await make_user(email="o@example.com", display_name="O")
    row = await connection_service.send_request(session, requester_id=me.id, addressee_id=other.id)
    r = await authed_client.post(f"/connection-requests/{row.id}/accept")
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_accept_already_accepted_returns_409(authed_client, make_user, session):
    me = authed_client.user  # type: ignore[attr-defined]
    requester = await make_user(email="r2@example.com", display_name="R2")
    row = await connection_service.send_request(
        session, requester_id=requester.id, addressee_id=me.id
    )
    await connection_service.accept(session, id=row.id, accepting_user_id=me.id)

    r = await authed_client.post(f"/connection-requests/{row.id}/accept")
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_accept_missing_returns_404(authed_client):
    r = await authed_client.post(f"/connection-requests/{uuid4()}/accept")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_delete_addressee_rejects_pending(authed_client, make_user, session):
    me = authed_client.user  # type: ignore[attr-defined]
    requester = await make_user(email="rj1@example.com", display_name="RJ1")
    row = await connection_service.send_request(
        session, requester_id=requester.id, addressee_id=me.id
    )
    r = await authed_client.delete(f"/connection-requests/{row.id}")
    assert r.status_code == 204


@pytest.mark.asyncio
async def test_delete_requester_cancels_pending(authed_client, make_user, session):
    me = authed_client.user  # type: ignore[attr-defined]
    other = await make_user(email="rj2@example.com", display_name="RJ2")
    row = await connection_service.send_request(session, requester_id=me.id, addressee_id=other.id)
    r = await authed_client.delete(f"/connection-requests/{row.id}")
    assert r.status_code == 204


@pytest.mark.asyncio
async def test_delete_by_third_party_returns_403(authed_client, make_user, session):
    a = await make_user(email="a3@example.com", display_name="A3")
    b = await make_user(email="b3@example.com", display_name="B3")
    row = await connection_service.send_request(session, requester_id=a.id, addressee_id=b.id)
    r = await authed_client.delete(f"/connection-requests/{row.id}")
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_delete_accepted_returns_404(authed_client, make_user, session):
    me = authed_client.user  # type: ignore[attr-defined]
    other = await make_user(email="ac@example.com", display_name="AC")
    row = await connection_service.send_request(session, requester_id=me.id, addressee_id=other.id)
    await connection_service.accept(session, id=row.id, accepting_user_id=other.id)

    r = await authed_client.delete(f"/connection-requests/{row.id}")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_delete_missing_returns_404(authed_client):
    r = await authed_client.delete(f"/connection-requests/{uuid4()}")
    assert r.status_code == 404
