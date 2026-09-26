"""Route tests for the per-kind push preferences and the per-show mute (NEU-1490)."""

from __future__ import annotations

import pytest
from fastapi import Response
from sqlalchemy import select

from tests.integration.routers.test_auth import _request
from tvbf.app.models import UserShowWatch
from tvbf.app.schemas import LoginRequest
from tvbf.catalog.models import Show
from tvbf.config import get_settings
from tvbf.routers import auth as auth_router

NOTIFY_FIELDS = (
    "notify_airs_today",
    "notify_premiere_set",
    "notify_premiere_moved",
    "notify_ended",
    "notify_revived",
)


async def _seed_show(session, *, show_id: int) -> Show:
    show = Show(id=show_id, name=f"Show-{show_id}", status="Ended")
    session.add(show)
    await session.flush()
    return show


# ---------------------------------------------------------------------------
# PATCH /me/preferences — the five notify_* flags
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_me_carries_every_notify_flag_defaulting_on(authed_client):
    r = await authed_client.get("/me")
    body = r.json()
    for field in NOTIFY_FIELDS:
        assert body[field] is True, field


@pytest.mark.asyncio
@pytest.mark.parametrize("field", NOTIFY_FIELDS)
async def test_patch_preferences_turns_off_one_kind_and_leaves_the_rest(authed_client, field):
    r = await authed_client.patch("/me/preferences", json={field: False})
    assert r.status_code == 200
    body = r.json()
    assert body[field] is False
    for other in NOTIFY_FIELDS:
        if other != field:
            assert body[other] is True, other
    assert body["activity_feed_enabled"] is True

    persisted = (await authed_client.get("/me")).json()
    assert persisted[field] is False


@pytest.mark.asyncio
async def test_patch_preferences_turns_a_kind_back_on(authed_client):
    await authed_client.patch("/me/preferences", json={"notify_ended": False})
    r = await authed_client.patch("/me/preferences", json={"notify_ended": True})
    assert r.json()["notify_ended"] is True


@pytest.mark.asyncio
async def test_patch_preferences_takes_notify_flags_beside_activity_feed(authed_client):
    r = await authed_client.patch(
        "/me/preferences",
        json={"activity_feed_enabled": False, "notify_airs_today": False},
    )
    body = r.json()
    assert body["activity_feed_enabled"] is False
    assert body["notify_airs_today"] is False
    assert body["notify_revived"] is True


@pytest.mark.asyncio
async def test_patch_preferences_rejects_a_non_boolean_flag(authed_client):
    r = await authed_client.patch("/me/preferences", json={"notify_ended": "sometimes"})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_login_response_carries_the_stored_notify_flags(session, make_user):
    user = await make_user(email="notify-lo@example.com", password="hunter2hunter2")
    user.notify_premiere_moved = False
    await session.commit()

    result = await auth_router.login(
        LoginRequest(email="notify-lo@example.com", password="hunter2hunter2"),
        _request(),
        Response(),
        db=session,
        settings=get_settings(),
    )
    assert result.notify_premiere_moved is False
    assert result.notify_airs_today is True


# ---------------------------------------------------------------------------
# PATCH /me/shows/{show_id}/mute
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mute_404_when_not_in_my_shows(authed_client, session):
    show = await _seed_show(session, show_id=974900)
    await session.commit()
    r = await authed_client.patch(f"/me/shows/{show.id}/mute", json={"muted": True})
    assert r.status_code == 404
    assert r.json()["detail"] == "not_in_my_shows"


@pytest.mark.asyncio
async def test_mute_requires_csrf(authed_client, session):
    me = authed_client.user  # type: ignore[attr-defined]
    show = await _seed_show(session, show_id=974905)
    session.add(UserShowWatch(user_id=me.id, show_id=show.id))
    await session.commit()

    r = await authed_client.patch(
        f"/me/shows/{show.id}/mute", json={"muted": True}, headers={"X-CSRF-Token": ""}
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_mute_toggles_and_is_reflected_in_my_shows(authed_client, session):
    me = authed_client.user  # type: ignore[attr-defined]
    show = await _seed_show(session, show_id=974910)
    session.add(UserShowWatch(user_id=me.id, show_id=show.id))
    await session.commit()

    def _entry(entries):
        return next(e for e in entries if e["show"]["id"] == show.id)

    before = _entry((await authed_client.get("/me/shows")).json())
    assert before["muted"] is False

    r = await authed_client.patch(f"/me/shows/{show.id}/mute", json={"muted": True})
    assert r.status_code == 204
    muted = _entry((await authed_client.get("/me/shows")).json())
    assert muted["muted"] is True
    assert muted["hide_from_activity"] is False

    r = await authed_client.patch(f"/me/shows/{show.id}/mute", json={"muted": False})
    assert r.status_code == 204
    assert _entry((await authed_client.get("/me/shows")).json())["muted"] is False


@pytest.mark.asyncio
async def test_mute_touches_only_the_callers_row(authed_client, make_user, session):
    me = authed_client.user  # type: ignore[attr-defined]
    other = await make_user(email="mute-other@example.com")
    show = await _seed_show(session, show_id=974920)
    session.add(UserShowWatch(user_id=me.id, show_id=show.id))
    session.add(UserShowWatch(user_id=other.id, show_id=show.id))
    await session.commit()

    await authed_client.patch(f"/me/shows/{show.id}/mute", json={"muted": True})

    others = (
        await session.execute(
            select(UserShowWatch.muted)
            .where(UserShowWatch.user_id == other.id, UserShowWatch.show_id == show.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert others is False
