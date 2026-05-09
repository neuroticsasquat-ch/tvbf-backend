"""Route tests for friend engagement endpoints (NEU-111).

GET /shows/{show_id}/friends
GET /episodes/{episode_id}/friends/watched
"""

from datetime import UTC, date, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from tvbf.app.models import UserEpisodeWatch, UserShowWatch
from tvbf.app.services import connection_service
from tvbf.main import app
from tvbf.tvmaze.models import Episode, Show


async def _seed_show(session, *, show_id: int, episodes: int = 1):
    show = Show(id=show_id, name="S", tvmaze_updated=1, status="Ended")
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


async def _accept_pair(session, a, b):
    req = await connection_service.send_request(session, requester_id=a.id, addressee_id=b.id)
    await connection_service.accept(session, id=req.id, accepting_user_id=b.id)


# ---------------------------------------------------------------------------
# Auth + 404
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_show_friends_requires_auth():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        r = await c.get("/shows/970000/friends")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_episode_friends_requires_auth():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        r = await c.get("/episodes/97000001/friends/watched")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_show_friends_404_for_unknown_show(authed_client):
    r = await authed_client.get("/shows/999999999/friends")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_episode_friends_404_for_unknown_episode(authed_client):
    r = await authed_client.get("/episodes/999999999/friends/watched")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Show endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_show_friends_returns_in_my_shows_and_watched(authed_client, make_user, session):
    me = authed_client.user  # type: ignore[attr-defined]
    tracker = await make_user(email="tracker@example.com", display_name="Tracker")
    watcher = await make_user(email="watcher@example.com", display_name="Watcher")
    both = await make_user(email="both@example.com", display_name="Both")
    await _accept_pair(session, me, tracker)
    await _accept_pair(session, me, watcher)
    await _accept_pair(session, me, both)
    show = await _seed_show(session, show_id=970001, episodes=2)
    session.add(UserShowWatch(user_id=tracker.id, show_id=show.id))
    session.add(UserShowWatch(user_id=both.id, show_id=show.id))
    session.add(
        UserEpisodeWatch(
            user_id=watcher.id,
            episode_id=show.id * 100 + 1,
            watched_at=datetime.now(UTC),
        )
    )
    session.add(
        UserEpisodeWatch(
            user_id=both.id,
            episode_id=show.id * 100 + 2,
            watched_at=datetime.now(UTC),
        )
    )
    await session.commit()

    r = await authed_client.get(f"/shows/{show.id}/friends")
    assert r.status_code == 200
    body = r.json()
    in_my = {u["id"] for u in body["in_my_shows"]}
    watched = {u["id"] for u in body["watched"]}
    assert in_my == {str(tracker.id), str(both.id)}
    assert watched == {str(watcher.id), str(both.id)}


@pytest.mark.asyncio
async def test_show_friends_excludes_non_connections(authed_client, make_user, session):
    me = authed_client.user  # type: ignore[attr-defined]
    pending_user = await make_user(email="pending@example.com", display_name="P")
    blocked_user = await make_user(email="blocked@example.com", display_name="B")
    stranger = await make_user(email="stranger@example.com", display_name="Str")
    # Pending: requester is the other party.
    await connection_service.send_request(session, requester_id=pending_user.id, addressee_id=me.id)
    # Blocked: caller blocks the user.
    await connection_service.block(session, blocker_id=me.id, blocked_id=blocked_user.id)
    show = await _seed_show(session, show_id=970002, episodes=1)
    # All three users have the show in their My Shows + watched ep 1.
    for u in (pending_user, blocked_user, stranger):
        session.add(UserShowWatch(user_id=u.id, show_id=show.id))
        session.add(
            UserEpisodeWatch(
                user_id=u.id,
                episode_id=show.id * 100 + 1,
                watched_at=datetime.now(UTC),
            )
        )
    await session.commit()

    r = await authed_client.get(f"/shows/{show.id}/friends")
    assert r.status_code == 200
    body = r.json()
    assert body["in_my_shows"] == []
    assert body["watched"] == []


@pytest.mark.asyncio
async def test_show_friends_returns_empty_arrays_when_no_matches(authed_client, make_user, session):
    me = authed_client.user  # type: ignore[attr-defined]
    friend = await make_user(email="f@example.com", display_name="F")
    await _accept_pair(session, me, friend)
    show = await _seed_show(session, show_id=970003, episodes=1)
    await session.commit()

    r = await authed_client.get(f"/shows/{show.id}/friends")
    assert r.status_code == 200
    assert r.json() == {"in_my_shows": [], "watched": []}


# ---------------------------------------------------------------------------
# Episode endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_episode_friends_returns_only_friends_who_watched_that_episode(
    authed_client, make_user, session
):
    me = authed_client.user  # type: ignore[attr-defined]
    a = await make_user(email="ea@example.com", display_name="EA")
    b = await make_user(email="eb@example.com", display_name="EB")
    await _accept_pair(session, me, a)
    await _accept_pair(session, me, b)
    show = await _seed_show(session, show_id=970010, episodes=2)
    # A watched ep1, B watched ep2.
    session.add(
        UserEpisodeWatch(
            user_id=a.id,
            episode_id=show.id * 100 + 1,
            watched_at=datetime.now(UTC),
        )
    )
    session.add(
        UserEpisodeWatch(
            user_id=b.id,
            episode_id=show.id * 100 + 2,
            watched_at=datetime.now(UTC),
        )
    )
    await session.commit()

    r1 = await authed_client.get(f"/episodes/{show.id * 100 + 1}/friends/watched")
    assert r1.status_code == 200
    assert {u["id"] for u in r1.json()} == {str(a.id)}

    r2 = await authed_client.get(f"/episodes/{show.id * 100 + 2}/friends/watched")
    assert r2.status_code == 200
    assert {u["id"] for u in r2.json()} == {str(b.id)}


@pytest.mark.asyncio
async def test_episode_friends_excludes_non_connections(authed_client, make_user, session):
    stranger = await make_user(email="stre@example.com", display_name="Stre")
    show = await _seed_show(session, show_id=970011, episodes=1)
    session.add(
        UserEpisodeWatch(
            user_id=stranger.id,
            episode_id=show.id * 100 + 1,
            watched_at=datetime.now(UTC),
        )
    )
    await session.commit()

    r = await authed_client.get(f"/episodes/{show.id * 100 + 1}/friends/watched")
    assert r.status_code == 200
    assert r.json() == []
