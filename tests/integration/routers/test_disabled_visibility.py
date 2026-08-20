"""A disabled user is invisible to everyone who is not already connected to
them, and every one of those surfaces returns when the flag is cleared
(NEU-1162 §4).

The restore half is tested beside the hide half everywhere, deliberately:
reversibility is the property this story buys, and it is the one a later
refactor could silently break while every "is it hidden?" assertion stayed
green.
"""

from datetime import UTC, date, datetime, timedelta

import pytest

from tvbf.app.models import ActivityEvent, UserShowWatch
from tvbf.app.services import connection_service
from tvbf.catalog.models import Episode, Show


async def _seed_show(session, *, show_id: int = 500, name: str = "Seeded") -> Show:
    show = Show(id=show_id, name=name, status="Ended")
    session.add(show)
    await session.flush()
    session.add(
        Episode(
            id=show_id * 100 + 1,
            show_id=show.id,
            season_number=1,
            episode_number=1,
            name="S1E1",
            air_date=date.today() - timedelta(days=10),
        )
    )
    await session.flush()
    return show


async def _befriend(session, a, b) -> None:
    req = await connection_service.send_request(session, requester_id=a.id, addressee_id=b.id)
    await connection_service.accept(session, id=req.id, accepting_user_id=b.id)


async def _set_disabled(session, user, value: datetime | None) -> None:
    user.disabled_at = value
    await session.commit()


@pytest.mark.asyncio
async def test_feed_drops_and_restores_a_disabled_friends_activity(
    authed_client, session, make_user
):
    me = authed_client.user  # type: ignore[attr-defined]
    friend = await make_user(email="friend@example.com", display_name="Friend", verified=True)
    await _befriend(session, me, friend)
    show = await _seed_show(session)
    session.add(
        ActivityEvent(
            actor_id=friend.id,
            verb="added_show",
            target_type="show",
            target_id=show.id,
            created_at=datetime.now(UTC),
        )
    )
    await session.commit()

    assert (await authed_client.get("/me/feed")).json()["items"] != []

    await _set_disabled(session, friend, datetime.now(UTC))
    assert (await authed_client.get("/me/feed")).json()["items"] == []

    await _set_disabled(session, friend, None)
    assert (await authed_client.get("/me/feed")).json()["items"] != []


@pytest.mark.asyncio
async def test_show_friends_drops_and_restores_a_disabled_friend(authed_client, session, make_user):
    me = authed_client.user  # type: ignore[attr-defined]
    friend = await make_user(email="friend@example.com", display_name="Friend", verified=True)
    await _befriend(session, me, friend)
    show = await _seed_show(session)
    session.add(UserShowWatch(user_id=friend.id, show_id=show.id))
    await session.commit()

    def watchers(body: dict) -> set[str]:
        return {u["id"] for u in body["in_my_shows"]} | {u["id"] for u in body["watched"]}

    r = await authed_client.get(f"/shows/{show.id}/friends")
    assert str(friend.id) in watchers(r.json())

    await _set_disabled(session, friend, datetime.now(UTC))
    r = await authed_client.get(f"/shows/{show.id}/friends")
    assert watchers(r.json()) == set()

    await _set_disabled(session, friend, None)
    r = await authed_client.get(f"/shows/{show.id}/friends")
    assert str(friend.id) in watchers(r.json())


@pytest.mark.asyncio
async def test_friend_library_404s_and_returns(authed_client, session, make_user):
    """Both `/users/{id}/shows` and `/users/{id}/watched`, via
    `_require_connected_friend` — 404 rather than 403, which is what makes
    "disabled" indistinguishable from "not your friend"."""
    me = authed_client.user  # type: ignore[attr-defined]
    friend = await make_user(email="friend@example.com", display_name="Friend", verified=True)
    await _befriend(session, me, friend)

    assert (await authed_client.get(f"/users/{friend.id}/shows")).status_code == 200
    assert (await authed_client.get(f"/users/{friend.id}/watched")).status_code == 200

    await _set_disabled(session, friend, datetime.now(UTC))
    assert (await authed_client.get(f"/users/{friend.id}/shows")).status_code == 404
    assert (await authed_client.get(f"/users/{friend.id}/watched")).status_code == 404

    await _set_disabled(session, friend, None)
    assert (await authed_client.get(f"/users/{friend.id}/shows")).status_code == 200
    assert (await authed_client.get(f"/users/{friend.id}/watched")).status_code == 200


@pytest.mark.asyncio
async def test_user_search_drops_and_restores_a_disabled_user(authed_client, session, make_user):
    """People discovery is where a new target is found."""
    target = await make_user(
        email="griefer@example.com", display_name="Griefer McGrief", verified=True
    )
    await session.commit()

    async def found() -> set[str]:
        r = await authed_client.get("/users/search", params={"q": "Griefer"})
        assert r.status_code == 200
        return {row["id"] for row in r.json()}

    assert str(target.id) in await found()

    await _set_disabled(session, target, datetime.now(UTC))
    assert await found() == set()

    await _set_disabled(session, target, None)
    assert str(target.id) in await found()


@pytest.mark.asyncio
async def test_pending_request_from_a_disabled_user_disappears_and_returns(
    authed_client, session, make_user
):
    """The request a griefer sent before being disabled is sitting in a
    stranger's inbox — the one piece of §4 that is not a read of their library."""
    me = authed_client.user  # type: ignore[attr-defined]
    griefer = await make_user(email="griefer@example.com", display_name="Griefer", verified=True)
    await connection_service.send_request(session, requester_id=griefer.id, addressee_id=me.id)
    await session.commit()

    async def incoming() -> set[str]:
        r = await authed_client.get("/me/connection-requests")
        assert r.status_code == 200
        return {row["requester"]["id"] for row in r.json()["incoming"]}

    assert str(griefer.id) in await incoming()

    await _set_disabled(session, griefer, datetime.now(UTC))
    assert await incoming() == set()

    await _set_disabled(session, griefer, None)
    assert str(griefer.id) in await incoming()


@pytest.mark.asyncio
async def test_outgoing_request_to_a_disabled_user_disappears(authed_client, session, make_user):
    """Both directions: an outgoing request to someone since disabled is a
    request that will never be answered."""
    me = authed_client.user  # type: ignore[attr-defined]
    target = await make_user(email="target@example.com", display_name="Target", verified=True)
    await connection_service.send_request(session, requester_id=me.id, addressee_id=target.id)
    await session.commit()

    async def outgoing() -> set[str]:
        r = await authed_client.get("/me/connection-requests")
        return {row["addressee"]["id"] for row in r.json()["outgoing"]}

    assert str(target.id) in await outgoing()
    await _set_disabled(session, target, datetime.now(UTC))
    assert await outgoing() == set()
    await _set_disabled(session, target, None)
    assert str(target.id) in await outgoing()


@pytest.mark.asyncio
async def test_accepted_connection_stays_listed_for_a_friend(authed_client, session, make_user):
    """AC 8 / §4.1. Invisibility to *strangers* is the goal, and an existing
    accepted friend is not a stranger: hiding the row makes a connection vanish
    and re-appear for someone who did nothing wrong."""
    me = authed_client.user  # type: ignore[attr-defined]
    friend = await make_user(email="friend@example.com", display_name="Friend", verified=True)
    await _befriend(session, me, friend)
    await _set_disabled(session, friend, datetime.now(UTC))

    r = await authed_client.get("/me/connections")
    assert r.status_code == 200
    assert str(friend.id) in {row["user"]["id"] for row in r.json()}
