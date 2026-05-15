"""Route tests for the cookie-session admin users endpoints (NEU-185)."""

import pytest
from httpx import ASGITransport, AsyncClient

from tvbf.main import app

# ---------------------------------------------------------------------------
# GET /admin/users
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_users_requires_auth():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        r = await c.get("/admin/users")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_list_users_forbidden_for_non_admin(authed_client):
    r = await authed_client.get("/admin/users")
    assert r.status_code == 403
    assert r.json()["detail"] == "admin_required"


@pytest.mark.asyncio
async def test_list_users_returns_every_user_for_admin(authed_client, session, make_user):
    me = authed_client.user  # type: ignore[attr-defined]
    me.is_admin = True
    await make_user(email="other@example.com", display_name="Other")
    await session.commit()

    r = await authed_client.get("/admin/users")
    assert r.status_code == 200
    body = r.json()
    emails = {row["email"] for row in body}
    assert "other@example.com" in emails
    me_row = next(row for row in body if row["id"] == str(me.id))
    assert me_row["is_admin"] is True


# ---------------------------------------------------------------------------
# PATCH /admin/users/{id}/admin
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patch_admin_flag_forbidden_for_non_admin(authed_client, session, make_user):
    target = await make_user(email="target@example.com", display_name="Target")
    await session.commit()
    r = await authed_client.patch(f"/admin/users/{target.id}/admin", json={"is_admin": True})
    assert r.status_code in (401, 403)


@pytest.mark.asyncio
async def test_patch_admin_flag_promotes_user(authed_client, session, make_user):
    me = authed_client.user  # type: ignore[attr-defined]
    me.is_admin = True
    target = await make_user(email="promote@example.com", display_name="P")
    await session.commit()

    r = await authed_client.patch(f"/admin/users/{target.id}/admin", json={"is_admin": True})
    assert r.status_code == 200
    assert r.json()["is_admin"] is True

    # Demote
    r2 = await authed_client.patch(f"/admin/users/{target.id}/admin", json={"is_admin": False})
    assert r2.status_code == 200
    assert r2.json()["is_admin"] is False


@pytest.mark.asyncio
async def test_patch_admin_flag_cannot_self_demote(authed_client, session):
    me = authed_client.user  # type: ignore[attr-defined]
    me.is_admin = True
    await session.commit()

    r = await authed_client.patch(f"/admin/users/{me.id}/admin", json={"is_admin": False})
    assert r.status_code == 403
    assert r.json()["detail"] == "cannot_demote_self"


@pytest.mark.asyncio
async def test_patch_admin_flag_404_for_missing_user(authed_client, session):
    me = authed_client.user  # type: ignore[attr-defined]
    me.is_admin = True
    await session.commit()

    r = await authed_client.patch(
        "/admin/users/00000000-0000-0000-0000-000000000000/admin",
        json={"is_admin": True},
    )
    assert r.status_code == 404
    assert r.json()["detail"] == "user_not_found"


# ---------------------------------------------------------------------------
# GET /me reflects is_admin
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_me_includes_is_admin(authed_client, session):
    r = await authed_client.get("/me")
    assert r.status_code == 200
    assert r.json()["is_admin"] is False

    me = authed_client.user  # type: ignore[attr-defined]
    me.is_admin = True
    await session.commit()

    r2 = await authed_client.get("/me")
    assert r2.json()["is_admin"] is True
