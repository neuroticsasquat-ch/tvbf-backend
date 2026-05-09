"""Route tests for friend library endpoints (NEU-108).

GET /users/{user_id}/shows
GET /users/{user_id}/watched
"""

from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from tvbf.app.models import UserEpisodeWatch, UserShowWatch
from tvbf.app.services import connection_service
from tvbf.main import app
from tvbf.tvmaze.models import Episode, Show


async def _seed_show(session, *, show_id: int, name: str = "S", episodes: int = 1):
    show = Show(id=show_id, name=name, tvmaze_updated=1, status="Ended")
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
                airdate=today - timedelta(days=episodes - i + 1),
            )
        )
    await session.flush()
    return show


@pytest.mark.asyncio
async def test_shows_requires_auth():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        r = await c.get(f"/users/{uuid4()}/shows")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_watched_requires_auth():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        r = await c.get(f"/users/{uuid4()}/watched")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_shows_returns_404_for_non_connection(authed_client, make_user):
    other = await make_user(email="stranger@example.com", display_name="Stranger")
    r = await authed_client.get(f"/users/{other.id}/shows")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_watched_returns_404_for_non_connection(authed_client, make_user):
    other = await make_user(email="stranger@example.com", display_name="Stranger")
    r = await authed_client.get(f"/users/{other.id}/watched")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_shows_returns_404_for_pending(authed_client, make_user, session):
    me = authed_client.user  # type: ignore[attr-defined]
    other = await make_user(email="pend@example.com", display_name="Pend")
    await connection_service.send_request(session, requester_id=me.id, addressee_id=other.id)
    r = await authed_client.get(f"/users/{other.id}/shows")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_shows_returns_404_for_blocked(authed_client, make_user, session):
    me = authed_client.user  # type: ignore[attr-defined]
    other = await make_user(email="blk@example.com", display_name="Blk")
    await connection_service.block(session, blocker_id=me.id, blocked_id=other.id)
    r = await authed_client.get(f"/users/{other.id}/shows")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_shows_returns_404_for_unknown_user(authed_client):
    r = await authed_client.get(f"/users/{uuid4()}/shows")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_shows_returns_404_for_self(authed_client):
    me_id = authed_client.user.id  # type: ignore[attr-defined]
    r = await authed_client.get(f"/users/{me_id}/shows")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_watched_returns_404_for_self(authed_client):
    me_id = authed_client.user.id  # type: ignore[attr-defined]
    r = await authed_client.get(f"/users/{me_id}/watched")
    assert r.status_code == 404


async def _accept_pair(session, a, b):
    req = await connection_service.send_request(session, requester_id=a.id, addressee_id=b.id)
    await connection_service.accept(session, id=req.id, accepting_user_id=b.id)


@pytest.mark.asyncio
async def test_shows_returns_friends_my_shows(authed_client, make_user, session):
    me = authed_client.user  # type: ignore[attr-defined]
    friend = await make_user(email="friend@example.com", display_name="Friend")
    await _accept_pair(session, me, friend)
    show = await _seed_show(session, show_id=950001, name="FriendShow")
    session.add(UserShowWatch(user_id=friend.id, show_id=show.id))
    await session.commit()

    r = await authed_client.get(f"/users/{friend.id}/shows")
    assert r.status_code == 200
    body = r.json()
    assert [row["show"]["id"] for row in body] == [show.id]
    assert r.headers.get("cache-control", "").startswith("private")


@pytest.mark.asyncio
async def test_watched_returns_friends_history(authed_client, make_user, session):
    me = authed_client.user  # type: ignore[attr-defined]
    friend = await make_user(email="friend@example.com", display_name="Friend")
    await _accept_pair(session, me, friend)
    show = await _seed_show(session, show_id=950002, name="FriendWatched")
    session.add(
        UserEpisodeWatch(
            user_id=friend.id,
            episode_id=show.id * 100 + 1,
            watched_at=datetime.now(UTC),
        )
    )
    await session.commit()

    r = await authed_client.get(f"/users/{friend.id}/watched")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    row = body[0]
    assert row["show"]["id"] == show.id
    assert row["status"] == "finished"


@pytest.mark.asyncio
async def test_friend_endpoints_dont_leak_callers_data(authed_client, make_user, session):
    """My own My Shows / watched data must not surface when querying a friend
    — these endpoints answer about the path user, not the caller."""
    me = authed_client.user  # type: ignore[attr-defined]
    friend = await make_user(email="friend@example.com", display_name="Friend")
    await _accept_pair(session, me, friend)
    my_show = await _seed_show(session, show_id=950003, name="MineOnly")
    friend_show = await _seed_show(session, show_id=950004, name="FriendOnly")
    session.add(UserShowWatch(user_id=me.id, show_id=my_show.id))
    session.add(UserShowWatch(user_id=friend.id, show_id=friend_show.id))
    await session.commit()

    r = await authed_client.get(f"/users/{friend.id}/shows")
    assert r.status_code == 200
    ids = [row["show"]["id"] for row in r.json()]
    assert my_show.id not in ids
    assert friend_show.id in ids
