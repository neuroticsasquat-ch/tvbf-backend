"""PATCH /admin/users/{user_id}/disabled — the moderation toggle (NEU-1162 §7.1)."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from tvbf.app.models import Session as SessionRow
from tvbf.app.repos import session_repo
from tvbf.app.tokens import new_session_id
from tvbf.main import app


async def _open_session_for(session, user) -> str:
    sess_id = new_session_id()
    await session_repo.create(
        session, session_id=sess_id, user_id=user.id, ttl_days=30, user_agent=None, ip=None
    )
    await session.commit()
    return sess_id


async def _session_ids_for(session, user) -> set[str]:
    rows = (
        await session.execute(select(SessionRow.id).where(SessionRow.user_id == user.id))
    ).scalars()
    return set(rows)


@pytest.mark.asyncio
async def test_requires_auth():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        r = await c.patch(f"/admin/users/{uuid4()}/disabled", json={"disabled": True})
    assert r.status_code in (401, 403)


@pytest.mark.asyncio
async def test_forbidden_for_non_admin(authed_client, session, make_user):
    target = await make_user(email="t@example.com", display_name="T")
    await session.commit()
    r = await authed_client.patch(f"/admin/users/{target.id}/disabled", json={"disabled": True})
    assert r.status_code == 403
    assert r.json()["detail"] == "admin_required"


@pytest.mark.asyncio
async def test_requires_csrf(authed_client, session, make_user):
    me = authed_client.user  # type: ignore[attr-defined]
    me.is_admin = True
    target = await make_user(email="t@example.com", display_name="T")
    await session.commit()
    r = await authed_client.patch(
        f"/admin/users/{target.id}/disabled",
        json={"disabled": True},
        headers={"X-CSRF-Token": ""},
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_unknown_user_is_404(authed_client, session):
    me = authed_client.user  # type: ignore[attr-defined]
    me.is_admin = True
    await session.commit()
    r = await authed_client.patch(f"/admin/users/{uuid4()}/disabled", json={"disabled": True})
    assert r.status_code == 404
    assert r.json()["detail"] == "user_not_found"


@pytest.mark.asyncio
async def test_disable_stamps_timestamp_and_revokes_sessions(authed_client, session, make_user):
    """AC 4: the flag is stamped, every session row for the target is gone, and
    the response carries the timestamp."""
    me = authed_client.user  # type: ignore[attr-defined]
    me.is_admin = True
    target = await make_user(email="abuser@example.com", display_name="Abuser")
    await session.commit()
    await _open_session_for(session, target)
    await _open_session_for(session, target)
    assert len(await _session_ids_for(session, target)) == 2

    r = await authed_client.patch(f"/admin/users/{target.id}/disabled", json={"disabled": True})
    assert r.status_code == 200, r.text
    assert r.json()["disabled_at"] is not None
    assert await _session_ids_for(session, target) == set()
    await session.refresh(target)
    assert target.disabled_at is not None


@pytest.mark.asyncio
async def test_enable_clears_timestamp(authed_client, session, make_user):
    me = authed_client.user  # type: ignore[attr-defined]
    me.is_admin = True
    target = await make_user(email="abuser@example.com", display_name="Abuser")
    target.disabled_at = datetime.now(UTC)
    await session.commit()

    r = await authed_client.patch(f"/admin/users/{target.id}/disabled", json={"disabled": False})
    assert r.status_code == 200, r.text
    assert r.json()["disabled_at"] is None
    await session.refresh(target)
    assert target.disabled_at is None


@pytest.mark.asyncio
async def test_re_disabling_keeps_the_original_stamp_and_still_revokes(
    authed_client, session, make_user
):
    """AC 4's second half. The stamp records when moderation began, and §1.1
    made it the only record of the act — re-stamping destroys that fact. The
    session delete runs unconditionally, closing the mint-between race."""
    me = authed_client.user  # type: ignore[attr-defined]
    me.is_admin = True
    original = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    target = await make_user(email="abuser@example.com", display_name="Abuser")
    target.disabled_at = original
    await session.commit()
    await _open_session_for(session, target)

    r = await authed_client.patch(f"/admin/users/{target.id}/disabled", json={"disabled": True})
    assert r.status_code == 200, r.text
    await session.refresh(target)
    assert target.disabled_at == original
    assert await _session_ids_for(session, target) == set()


@pytest.mark.asyncio
async def test_admin_cannot_disable_themselves(authed_client, session):
    """AC 5, mirroring the existing `cannot_demote_self` guard."""
    me = authed_client.user  # type: ignore[attr-defined]
    me.is_admin = True
    await session.commit()
    r = await authed_client.patch(f"/admin/users/{me.id}/disabled", json={"disabled": True})
    assert r.status_code == 403
    assert r.json()["detail"] == "cannot_disable_self"
    await session.refresh(me)
    assert me.disabled_at is None


@pytest.mark.asyncio
async def test_admin_may_re_enable_themselves(authed_client, session):
    """The self-guard covers disabling only — `{"disabled": false}` on yourself
    is not the mistake it exists to prevent."""
    me = authed_client.user  # type: ignore[attr-defined]
    me.is_admin = True
    await session.commit()
    r = await authed_client.patch(f"/admin/users/{me.id}/disabled", json={"disabled": False})
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_admin_may_disable_another_admin(authed_client, session, make_user):
    """AC 5's other half: blocking this would protect a rogue admin from the
    only remedy that exists."""
    me = authed_client.user  # type: ignore[attr-defined]
    me.is_admin = True
    other_admin = await make_user(email="admin2@example.com", display_name="Admin Two")
    other_admin.is_admin = True
    await session.commit()

    r = await authed_client.patch(
        f"/admin/users/{other_admin.id}/disabled", json={"disabled": True}
    )
    assert r.status_code == 200, r.text
    assert r.json()["disabled_at"] is not None


@pytest.mark.asyncio
async def test_admin_list_carries_disabled_at(authed_client, session, make_user):
    """AC 8/13: the admin list shows the state; `UserOut` / `AuthedUserOut`
    do not carry it."""
    me = authed_client.user  # type: ignore[attr-defined]
    me.is_admin = True
    target = await make_user(email="abuser@example.com", display_name="Abuser")
    target.disabled_at = datetime.now(UTC)
    await session.commit()

    r = await authed_client.get("/admin/users")
    assert r.status_code == 200
    rows = {row["id"]: row for row in r.json()}
    assert rows[str(target.id)]["disabled_at"] is not None
    assert rows[str(me.id)]["disabled_at"] is None

    # `/me` returns `AuthedUserOut`, which **inherits** `UserOut` — so a field
    # absent here is absent from both, and no live route returns a bare
    # `UserOut`. The schema-level assertion states that rather than leaving it
    # to be re-derived.
    from tvbf.app.schemas import AuthedUserOut, UserOut

    assert "disabled_at" not in UserOut.model_fields
    assert "disabled_at" not in AuthedUserOut.model_fields

    me_row = await authed_client.get("/me")
    assert me_row.status_code == 200
    assert "disabled_at" not in me_row.json()
