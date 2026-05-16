"""Route tests for the activity privacy toggles + feed filtering (NEU-180)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from tvbf.app.models import ActivityEvent, UserShowWatch
from tvbf.app.services import connection_service
from tvbf.main import app
from tvbf.tvmaze.models import Episode, Show


async def _seed_show(session, *, show_id: int, episodes: int = 1) -> Show:
    show = Show(id=show_id, name=f"Show-{show_id}", tvmaze_updated=1, status="Ended")
    session.add(show)
    await session.flush()
    today = date.today()
    for i in range(1, episodes + 1):
        session.add(
            Episode(
                id=show_id * 100 + i,
                show_id=show.id,
                season=1,
                number=i,
                airdate=today - timedelta(days=10),
            )
        )
    await session.flush()
    return show


async def _accept_pair(session, a, b):
    req = await connection_service.send_request(session, requester_id=a.id, addressee_id=b.id)
    await connection_service.accept(session, id=req.id, accepting_user_id=b.id)


def _event(*, actor_id, verb, target_type, target_id, season_number=None, payload=None):
    return ActivityEvent(
        id=uuid4(),
        actor_id=actor_id,
        verb=verb,
        target_type=target_type,
        target_id=target_id,
        season_number=season_number,
        payload=payload,
        created_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# PATCH /me/preferences
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patch_preferences_requires_auth():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        r = await c.patch("/me/preferences", json={"activity_feed_enabled": False})
    assert r.status_code in (401, 403)


@pytest.mark.asyncio
async def test_patch_preferences_toggles_flag_and_is_reflected_in_get_me(authed_client):
    r = await authed_client.get("/me")
    assert r.json()["activity_feed_enabled"] is True

    r2 = await authed_client.patch("/me/preferences", json={"activity_feed_enabled": False})
    assert r2.status_code == 200
    assert r2.json()["activity_feed_enabled"] is False

    r3 = await authed_client.get("/me")
    assert r3.json()["activity_feed_enabled"] is False


@pytest.mark.asyncio
async def test_patch_preferences_empty_body_is_noop(authed_client):
    r = await authed_client.patch("/me/preferences", json={})
    assert r.status_code == 200
    assert r.json()["activity_feed_enabled"] is True


# ---------------------------------------------------------------------------
# PATCH /me/shows/{show_id}/hide-from-activity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hide_from_activity_404_when_not_in_my_shows(authed_client, session):
    show = await _seed_show(session, show_id=974000)
    await session.commit()
    r = await authed_client.patch(
        f"/me/shows/{show.id}/hide-from-activity", json={"hide_from_activity": True}
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_hide_from_activity_toggles_flag(authed_client, session):
    me = authed_client.user  # type: ignore[attr-defined]
    show = await _seed_show(session, show_id=974010)
    session.add(UserShowWatch(user_id=me.id, show_id=show.id))
    await session.commit()

    r = await authed_client.patch(
        f"/me/shows/{show.id}/hide-from-activity", json={"hide_from_activity": True}
    )
    assert r.status_code == 204

    # Reflected in GET /me/shows
    r2 = await authed_client.get("/me/shows")
    entries = r2.json()
    assert any(e["show"]["id"] == show.id and e["hide_from_activity"] is True for e in entries)


# ---------------------------------------------------------------------------
# Feed filtering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_feed_excludes_actor_with_global_toggle_off(authed_client, make_user, session):
    me = authed_client.user  # type: ignore[attr-defined]
    friend = await make_user(email="off@example.com", display_name="Off")
    await _accept_pair(session, me, friend)
    show = await _seed_show(session, show_id=974020)
    session.add(
        _event(actor_id=friend.id, verb="added_show", target_type="show", target_id=show.id)
    )
    await session.commit()

    # Visible by default.
    r = await authed_client.get("/me/feed")
    assert len(r.json()["items"]) == 1

    # Toggle friend's broadcast off.
    friend.activity_feed_enabled = False
    await session.commit()

    r = await authed_client.get("/me/feed")
    assert r.json()["items"] == []

    # Toggling back on restores the row (soft filter).
    friend.activity_feed_enabled = True
    await session.commit()

    r = await authed_client.get("/me/feed")
    assert len(r.json()["items"]) == 1


@pytest.mark.asyncio
async def test_feed_excludes_show_with_hide_from_activity(authed_client, make_user, session):
    me = authed_client.user  # type: ignore[attr-defined]
    friend = await make_user(email="hide@example.com", display_name="Hide")
    await _accept_pair(session, me, friend)
    show_hidden = await _seed_show(session, show_id=974030, episodes=1)
    show_visible = await _seed_show(session, show_id=974031, episodes=1)
    session.add(UserShowWatch(user_id=friend.id, show_id=show_hidden.id))
    session.add(UserShowWatch(user_id=friend.id, show_id=show_visible.id))

    ep_hidden = show_hidden.id * 100 + 1
    ep_visible = show_visible.id * 100 + 1
    session.add_all(
        [
            _event(
                actor_id=friend.id,
                verb="added_show",
                target_type="show",
                target_id=show_hidden.id,
            ),
            _event(
                actor_id=friend.id,
                verb="watched_episode",
                target_type="episode",
                target_id=ep_hidden,
            ),
            _event(
                actor_id=friend.id,
                verb="watched_season",
                target_type="show",
                target_id=show_hidden.id,
                season_number=1,
            ),
            _event(
                actor_id=friend.id,
                verb="rated_show",
                target_type="show",
                target_id=show_hidden.id,
                payload={"stars": 5.0},
            ),
            _event(
                actor_id=friend.id,
                verb="rated_episode",
                target_type="episode",
                target_id=ep_hidden,
                payload={"stars": 4.0},
            ),
            _event(
                actor_id=friend.id,
                verb="added_show",
                target_type="show",
                target_id=show_visible.id,
            ),
            _event(
                actor_id=friend.id,
                verb="watched_episode",
                target_type="episode",
                target_id=ep_visible,
            ),
        ]
    )
    await session.commit()

    r = await authed_client.get("/me/feed")
    assert len(r.json()["items"]) == 7

    # Friend hides show_hidden from their activity broadcast.
    from sqlalchemy import update as sa_update

    await session.execute(
        sa_update(UserShowWatch)
        .where(UserShowWatch.user_id == friend.id, UserShowWatch.show_id == show_hidden.id)
        .values(hide_from_activity=True)
    )
    await session.commit()

    r = await authed_client.get("/me/feed")
    items = r.json()["items"]
    show_ids_in_feed = {i["show"]["id"] for i in items if i["show"]}
    assert show_ids_in_feed == {show_visible.id}
