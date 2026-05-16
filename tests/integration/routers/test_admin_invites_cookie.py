"""Route tests for the cookie-session admin invite endpoints (NEU-187)."""

import pytest
from httpx import ASGITransport, AsyncClient

from tvbf.main import app

# ---------------------------------------------------------------------------
# POST /admin/invites/email
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_invite_email_requires_auth():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        r = await c.post("/admin/invites/email", json={"email": "alice@example.com"})
    assert r.status_code in (401, 403)


@pytest.mark.asyncio
async def test_post_invite_email_forbidden_for_non_admin(authed_client):
    r = await authed_client.post("/admin/invites/email", json={"email": "alice@example.com"})
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_post_invite_email_creates_row_and_sends_email(
    authed_client, session, _stub_outbound_email
):
    me = authed_client.user  # type: ignore[attr-defined]
    me.is_admin = True
    await session.commit()

    r = await authed_client.post("/admin/invites/email", json={"email": "newbie@example.com"})
    assert r.status_code == 200
    body = r.json()
    assert body["email_hint"] == "newbie@example.com"
    assert body["consumed_at"] is None
    code = body["code"]
    assert code

    # The captured email is the most recent one.
    captured = _stub_outbound_email
    assert captured, "no email captured"
    sent = captured[-1]
    assert sent["to"] == "newbie@example.com"
    assert code in sent["text"]
    assert code in sent["html"]
    assert "newbie@example.com" in sent["text"]
    # Signup link carries both query params.
    assert f"invite={code}" in sent["text"]
    assert "email=newbie%40example.com" in sent["text"]


@pytest.mark.asyncio
async def test_post_invite_email_rejects_invalid_email(authed_client, session):
    me = authed_client.user  # type: ignore[attr-defined]
    me.is_admin = True
    await session.commit()
    r = await authed_client.post("/admin/invites/email", json={"email": "not-an-email"})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# GET /admin/invites/cookie
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_invites_cookie_forbidden_for_non_admin(authed_client):
    r = await authed_client.get("/admin/invites/cookie")
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_list_invites_cookie_returns_list_for_admin(
    authed_client, session, _stub_outbound_email
):
    me = authed_client.user  # type: ignore[attr-defined]
    me.is_admin = True
    await session.commit()

    await authed_client.post("/admin/invites/email", json={"email": "a@example.com"})
    await authed_client.post("/admin/invites/email", json={"email": "b@example.com"})

    r = await authed_client.get("/admin/invites/cookie")
    assert r.status_code == 200
    hints = {row["email_hint"] for row in r.json()}
    assert {"a@example.com", "b@example.com"} <= hints
