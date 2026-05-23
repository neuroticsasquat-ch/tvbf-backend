"""Route tests for friend ratings endpoints (NEU-168).

GET /shows/{show_id}/friends/ratings
GET /episodes/{episode_id}/friends/ratings
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient

from tvbf.app.models import UserEpisodeRating, UserShowRating
from tvbf.app.services import connection_service
from tvbf.main import app
from tvbf.tvmaze.models import Episode, Show


async def _seed_show(session, *, show_id: int, episodes: int = 1):
    show = Show(id=show_id, name="S", tvmaze_updated=1, status="Ended")
    session.add(show)
    await session.flush()
    for i in range(1, episodes + 1):
        session.add(
            Episode(
                id=show_id * 100 + i,
                show_id=show.id,
                season=1,
                number=i,
            )
        )
    await session.flush()
    return show


async def _accept_pair(session, a, b):
    req = await connection_service.send_request(session, requester_id=a.id, addressee_id=b.id)
    await connection_service.accept(session, id=req.id, accepting_user_id=b.id)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_show_friend_ratings_requires_auth():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        r = await c.get("/shows/980000/friends/ratings")
    assert r.status_code in (401, 403)


@pytest.mark.asyncio
async def test_episode_friend_ratings_requires_auth():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        r = await c.get("/episodes/98000001/friends/ratings")
    assert r.status_code in (401, 403)


# ---------------------------------------------------------------------------
# 404
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_show_friend_ratings_404_for_unknown_show(authed_client):
    r = await authed_client.get("/shows/999999991/friends/ratings")
    assert r.status_code == 404
    assert r.json()["detail"] == "show_not_found"


@pytest.mark.asyncio
async def test_episode_friend_ratings_404_for_unknown_episode(authed_client):
    r = await authed_client.get("/episodes/999999991/friends/ratings")
    assert r.status_code == 404
    assert r.json()["detail"] == "episode_not_found"


# ---------------------------------------------------------------------------
# Show endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_show_friend_ratings_aggregates_accepted_only(authed_client, make_user, session):
    me = authed_client.user  # type: ignore[attr-defined]
    friend_a = await make_user(email="fra@example.com", display_name="FriendA")
    friend_b = await make_user(email="frb@example.com", display_name="FriendB")
    friend_c = await make_user(email="frc@example.com", display_name="FriendC")
    pending_user = await make_user(email="frp@example.com", display_name="Pending")
    blocked_user = await make_user(email="frbk@example.com", display_name="Blocked")
    stranger = await make_user(email="frs@example.com", display_name="Stranger")

    await _accept_pair(session, me, friend_a)
    await _accept_pair(session, me, friend_b)
    await _accept_pair(session, me, friend_c)
    # Pending request from the other party.
    await connection_service.send_request(session, requester_id=pending_user.id, addressee_id=me.id)
    # Caller blocks blocked_user.
    await connection_service.block(session, blocker_id=me.id, blocked_id=blocked_user.id)

    show = await _seed_show(session, show_id=980001, episodes=1)

    now = datetime.now(UTC)
    # Accepted friends — ratings with distinct rated_at to assert ordering.
    session.add(
        UserShowRating(
            user_id=friend_a.id,
            show_id=show.id,
            stars=Decimal("3.0"),
            rated_at=now - timedelta(minutes=30),
        )
    )
    session.add(
        UserShowRating(
            user_id=friend_b.id,
            show_id=show.id,
            stars=Decimal("4.0"),
            rated_at=now - timedelta(minutes=10),
        )
    )
    session.add(
        UserShowRating(
            user_id=friend_c.id,
            show_id=show.id,
            stars=Decimal("5.0"),
            rated_at=now - timedelta(minutes=20),
        )
    )
    # Non-friends (must be excluded).
    session.add(UserShowRating(user_id=pending_user.id, show_id=show.id, stars=Decimal("1.0")))
    session.add(UserShowRating(user_id=blocked_user.id, show_id=show.id, stars=Decimal("1.0")))
    session.add(UserShowRating(user_id=stranger.id, show_id=show.id, stars=Decimal("1.0")))
    await session.commit()

    r = await authed_client.get(f"/shows/{show.id}/friends/ratings")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 3
    # avg = (3.0 + 4.0 + 5.0) / 3 = 4.0, rounded to 1 decimal.
    assert body["avg"] == 4.0
    items = body["items"]
    assert len(items) == 3
    user_ids = [i["user_id"] for i in items]
    assert str(pending_user.id) not in user_ids
    assert str(blocked_user.id) not in user_ids
    assert str(stranger.id) not in user_ids
    # Order: friend_b (newest) -> friend_c -> friend_a (oldest).
    assert user_ids == [str(friend_b.id), str(friend_c.id), str(friend_a.id)]
    # display_name carries through.
    names_by_id = {i["user_id"]: i["display_name"] for i in items}
    assert names_by_id[str(friend_a.id)] == "FriendA"
    assert names_by_id[str(friend_b.id)] == "FriendB"
    assert names_by_id[str(friend_c.id)] == "FriendC"


@pytest.mark.asyncio
async def test_show_friend_ratings_empty_when_no_friend_ratings(authed_client, session):
    show = await _seed_show(session, show_id=980002, episodes=1)
    await session.commit()

    r = await authed_client.get(f"/shows/{show.id}/friends/ratings")
    assert r.status_code == 200
    body = r.json()
    assert body == {"avg": None, "count": 0, "items": []}


# ---------------------------------------------------------------------------
# Episode endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_episode_friend_ratings_aggregates_accepted_only(authed_client, make_user, session):
    me = authed_client.user  # type: ignore[attr-defined]
    friend_a = await make_user(email="era@example.com", display_name="EFriendA")
    friend_b = await make_user(email="erb@example.com", display_name="EFriendB")
    friend_c = await make_user(email="erc@example.com", display_name="EFriendC")
    pending_user = await make_user(email="erp@example.com", display_name="EPending")
    blocked_user = await make_user(email="erbk@example.com", display_name="EBlocked")
    stranger = await make_user(email="ers@example.com", display_name="EStranger")

    await _accept_pair(session, me, friend_a)
    await _accept_pair(session, me, friend_b)
    await _accept_pair(session, me, friend_c)
    await connection_service.send_request(session, requester_id=pending_user.id, addressee_id=me.id)
    await connection_service.block(session, blocker_id=me.id, blocked_id=blocked_user.id)

    show = await _seed_show(session, show_id=980010, episodes=1)
    episode_id = show.id * 100 + 1

    now = datetime.now(UTC)
    session.add(
        UserEpisodeRating(
            user_id=friend_a.id,
            episode_id=episode_id,
            stars=Decimal("4.5"),
            rated_at=now - timedelta(minutes=30),
        )
    )
    session.add(
        UserEpisodeRating(
            user_id=friend_b.id,
            episode_id=episode_id,
            stars=Decimal("2.5"),
            rated_at=now - timedelta(minutes=5),
        )
    )
    session.add(
        UserEpisodeRating(
            user_id=friend_c.id,
            episode_id=episode_id,
            stars=Decimal("3.5"),
            rated_at=now - timedelta(minutes=15),
        )
    )
    session.add(
        UserEpisodeRating(user_id=pending_user.id, episode_id=episode_id, stars=Decimal("1.0"))
    )
    session.add(
        UserEpisodeRating(user_id=blocked_user.id, episode_id=episode_id, stars=Decimal("1.0"))
    )
    session.add(UserEpisodeRating(user_id=stranger.id, episode_id=episode_id, stars=Decimal("1.0")))
    await session.commit()

    r = await authed_client.get(f"/episodes/{episode_id}/friends/ratings")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 3
    # avg = (4.5 + 2.5 + 3.5) / 3 = 3.5
    assert body["avg"] == 3.5
    items = body["items"]
    assert len(items) == 3
    user_ids = [i["user_id"] for i in items]
    assert str(pending_user.id) not in user_ids
    assert str(blocked_user.id) not in user_ids
    assert str(stranger.id) not in user_ids
    # Order: friend_b (newest) -> friend_c -> friend_a (oldest).
    assert user_ids == [str(friend_b.id), str(friend_c.id), str(friend_a.id)]
    names_by_id = {i["user_id"]: i["display_name"] for i in items}
    assert names_by_id[str(friend_a.id)] == "EFriendA"


@pytest.mark.asyncio
async def test_episode_friend_ratings_empty_when_no_friend_ratings(authed_client, session):
    show = await _seed_show(session, show_id=980011, episodes=1)
    await session.commit()

    r = await authed_client.get(f"/episodes/{show.id * 100 + 1}/friends/ratings")
    assert r.status_code == 200
    body = r.json()
    assert body == {"avg": None, "count": 0, "items": []}
