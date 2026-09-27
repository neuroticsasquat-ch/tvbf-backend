"""Contract tests for `GET /me/friends/popular` (NEU-1499, project spec §5.2, §8).

The ranking itself — window, tie-break, both sharing switches, the read-time
adult/tombstone filter — is pinned at repo level in
`tests/integration/app/repos/test_popular_shows_for_friends.py`. These cases are
what the route adds on top: auth, the header, the envelope and its two empty
bodies, the friend graph going through `accepted_friend_ids`, hydration, order,
and a statement count that does not move with the list.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from tvbf.app.models import ActivityEvent, UserShowRating, UserShowWatch
from tvbf.app.services import connection_service
from tvbf.catalog.models import Show
from tvbf.main import app

_URL = "/me/friends/popular"


async def _seed_shows(session, *show_ids: int) -> None:
    for show_id in show_ids:
        session.add(Show(id=show_id, name=f"Show-{show_id}", status="Ended"))
    await session.flush()


async def _accept(session, a, b) -> None:
    req = await connection_service.send_request(session, requester_id=a.id, addressee_id=b.id)
    await connection_service.accept(session, id=req.id, accepting_user_id=b.id)


def _event(session, *, actor, target_id: int, verb: str = "added_show") -> None:
    session.add(
        ActivityEvent(
            id=uuid4(),
            actor_id=actor.id,
            verb=verb,
            target_type="show",
            target_id=target_id,
            created_at=datetime.now(UTC) - timedelta(hours=1),
        )
    )


async def _friends(session, make_user, me, n: int):
    friends = [await make_user(email=f"pop{i}@example.com") for i in range(n)]
    for f in friends:
        await _accept(session, me, f)
    return friends


def _ids(body) -> list[int]:
    return [s["id"] for s in body["shows"]]


@pytest.mark.asyncio
async def test_requires_auth():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        r = await c.get(_URL)
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_no_connections_is_the_first_empty_body(authed_client):
    r = await authed_client.get(_URL)
    assert r.status_code == 200
    assert r.headers["cache-control"] == "private, no-store"
    assert r.json() == {"window_days": 14, "connection_count": 0, "shows": []}


@pytest.mark.asyncio
async def test_quiet_connections_is_the_second_empty_body(authed_client, session, make_user):
    me = authed_client.user  # type: ignore[attr-defined]
    await _friends(session, make_user, me, 2)
    await session.commit()

    r = await authed_client.get(_URL)
    assert r.status_code == 200
    assert r.headers["cache-control"] == "private, no-store"
    assert r.json() == {"window_days": 14, "connection_count": 2, "shows": []}


@pytest.mark.asyncio
async def test_serves_the_full_shape_with_friend_count(authed_client, session, make_user):
    me = authed_client.user  # type: ignore[attr-defined]
    a, b = await _friends(session, make_user, me, 2)
    await _seed_shows(session, 811001)
    _event(session, actor=a, target_id=811001)
    _event(session, actor=b, target_id=811001, verb="rated_show")
    await session.commit()

    r = await authed_client.get(_URL)
    assert r.status_code == 200
    assert r.headers["cache-control"] == "private, no-store"
    body = r.json()
    assert body["window_days"] == 14
    assert body["connection_count"] == 2
    (show,) = body["shows"]
    assert show["id"] == 811001
    assert show["friend_count"] == 2
    assert show["in_my_shows"] is False
    assert show["my_rating"] is None
    assert show["genres"] == []
    assert show["network"] is None
    # Ordering keys stay internal (spec §5.2).
    assert "activity_count" not in show
    assert "last_activity_at" not in show


@pytest.mark.asyncio
async def test_pending_blocked_disabled_and_self_contribute_nothing(
    authed_client, session, make_user
):
    me = authed_client.user  # type: ignore[attr-defined]
    (friend,) = await _friends(session, make_user, me, 1)
    pending = await make_user(email="pending@example.com")
    blocked = await make_user(email="blocked@example.com")
    disabled = await make_user(email="disabled@example.com")
    stranger = await make_user(email="stranger@example.com")
    await connection_service.send_request(session, requester_id=me.id, addressee_id=pending.id)
    await connection_service.block(session, blocker_id=me.id, blocked_id=blocked.id)
    await _accept(session, me, disabled)
    disabled.disabled_at = datetime.now(UTC)

    await _seed_shows(session, 1, 2, 3, 4, 5, 6)
    _event(session, actor=friend, target_id=1)
    _event(session, actor=pending, target_id=2)
    _event(session, actor=blocked, target_id=3)
    _event(session, actor=disabled, target_id=4)
    _event(session, actor=stranger, target_id=5)
    _event(session, actor=me, target_id=6)
    await session.commit()

    body = (await authed_client.get(_URL)).json()
    assert _ids(body) == [1]
    # Only the accepted, enabled friend counts as a connection.
    assert body["connection_count"] == 1


@pytest.mark.asyncio
async def test_a_friend_with_everything_hidden_still_counts_as_a_connection(
    authed_client, session, make_user
):
    me = authed_client.user  # type: ignore[attr-defined]
    globally_hidden, per_show_hidden = await _friends(session, make_user, me, 2)
    await _seed_shows(session, 1, 2)
    _event(session, actor=globally_hidden, target_id=1)
    _event(session, actor=per_show_hidden, target_id=2)
    globally_hidden.activity_feed_enabled = False
    session.add(UserShowWatch(user_id=per_show_hidden.id, show_id=2, hide_from_activity=True))
    await session.commit()

    assert (await authed_client.get(_URL)).json() == {
        "window_days": 14,
        "connection_count": 2,
        "shows": [],
    }


@pytest.mark.asyncio
async def test_hydrates_in_my_shows_and_my_rating(authed_client, session, make_user):
    me = authed_client.user  # type: ignore[attr-defined]
    (friend,) = await _friends(session, make_user, me, 1)
    await _seed_shows(session, 1, 2)
    _event(session, actor=friend, target_id=1)
    _event(session, actor=friend, target_id=1, verb="rated_show")
    _event(session, actor=friend, target_id=2)
    # Tracked is a mark, never a filter: show 1 still appears.
    session.add(UserShowWatch(user_id=me.id, show_id=1))
    session.add(UserShowRating(user_id=me.id, show_id=1, stars=4.5))
    await session.commit()

    body = (await authed_client.get(_URL)).json()
    by_id = {s["id"]: s for s in body["shows"]}
    assert by_id[1]["in_my_shows"] is True
    assert by_id[1]["my_rating"] == 4.5
    assert by_id[2]["in_my_shows"] is False
    assert by_id[2]["my_rating"] is None


@pytest.mark.asyncio
async def test_preserves_the_query_order(authed_client, session, make_user):
    """Rank order is the server's: three friends on show 30, two on 10, one each
    on 40 and 20 — 40 ahead on activity count. That disagrees with id order,
    insertion order, and a client-style re-sort by `friend_count` then id."""
    me = authed_client.user  # type: ignore[attr-defined]
    a, b, c = await _friends(session, make_user, me, 3)
    await _seed_shows(session, 10, 20, 30, 40)
    _event(session, actor=a, target_id=20)
    _event(session, actor=b, target_id=40)
    _event(session, actor=b, target_id=40, verb="rated_show")
    for f in (a, b):
        _event(session, actor=f, target_id=10)
    for f in (a, b, c):
        _event(session, actor=f, target_id=30)
    await session.commit()

    body = (await authed_client.get(_URL)).json()
    assert _ids(body) == [30, 10, 40, 20]
    assert [s["friend_count"] for s in body["shows"]] == [3, 2, 1, 1]


@pytest.mark.asyncio
async def test_serves_the_list_in_a_fixed_number_of_statements(authed_client, session, make_user):
    """Friends, the ranking, the show rows, memberships and ratings — none of
    them per row. Counted over every statement, so an accidental per-row load
    moves the number."""
    from sqlalchemy import event

    from tvbf.db import engine as app_engine

    me = authed_client.user  # type: ignore[attr-defined]
    (friend,) = await _friends(session, make_user, me, 1)
    await _seed_shows(session, *range(1, 11))
    _event(session, actor=friend, target_id=1)
    await session.commit()

    statements: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    # The route runs on the app's own engine (`get_session`), not on the test
    # session's — listening on the wrong one silently counts zero.
    engine = app_engine.sync_engine
    event.listen(engine, "before_cursor_execute", _record)
    try:
        one = await authed_client.get(_URL)
        for_one = list(statements)
        statements.clear()

        for show_id in range(2, 11):
            _event(session, actor=friend, target_id=show_id)
        await session.commit()
        statements.clear()

        ten = await authed_client.get(_URL)
        for_ten = list(statements)
    finally:
        event.remove(engine, "before_cursor_execute", _record)

    assert len(one.json()["shows"]) == 1
    assert len(ten.json()["shows"]) == 10
    assert len(for_one) == len(for_ten), (for_one, for_ten)
    # Spec §5.2: one ranking query, then three hydration queries (show rows,
    # memberships, ratings). The friends lookup and auth are outside the number.
    payload = [
        s
        for s in for_ten
        if any(
            k in s
            for k in ("activity_event", "FROM catalog.show", "user_show_watch", "user_show_rating")
        )
    ]
    assert len(payload) == 4, payload
