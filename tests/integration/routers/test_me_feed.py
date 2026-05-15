"""Route tests for GET /me/feed (NEU-178)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from tvbf.app.models import ActivityEvent
from tvbf.app.services import connection_service
from tvbf.app.services.feed_service import encode_cursor
from tvbf.main import app
from tvbf.tvmaze.models import Episode, Show


async def _seed_show(session, *, show_id: int, episodes_per_season=(2,)) -> Show:
    show = Show(id=show_id, name=f"Show-{show_id}", tvmaze_updated=1, status="Ended")
    session.add(show)
    await session.flush()
    today = date.today()
    for season_idx, n in enumerate(episodes_per_season, start=1):
        for ep_num in range(1, n + 1):
            session.add(
                Episode(
                    id=show_id * 100 + season_idx * 10 + ep_num,
                    show_id=show.id,
                    season=season_idx,
                    number=ep_num,
                    name=f"S{season_idx}E{ep_num}",
                    airdate=today - timedelta(days=10),
                )
            )
    await session.flush()
    return show


async def _accept_pair(session, a, b):
    req = await connection_service.send_request(session, requester_id=a.id, addressee_id=b.id)
    await connection_service.accept(session, id=req.id, accepting_user_id=b.id)


def _make_event(
    *,
    actor_id,
    verb: str,
    target_type: str,
    target_id: int,
    created_at: datetime,
    season_number: int | None = None,
    payload: dict | None = None,
) -> ActivityEvent:
    return ActivityEvent(
        id=uuid4(),
        actor_id=actor_id,
        verb=verb,
        target_type=target_type,
        target_id=target_id,
        season_number=season_number,
        payload=payload,
        created_at=created_at,
    )


# ---------------------------------------------------------------------------
# Auth + cursor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_feed_requires_auth():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        r = await c.get("/me/feed")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_feed_malformed_cursor_400(authed_client):
    r = await authed_client.get("/me/feed", params={"cursor": "@@@not-base64@@@"})
    assert r.status_code == 400
    assert r.json()["detail"] == "invalid_cursor"


@pytest.mark.asyncio
async def test_feed_empty_when_no_friends(authed_client):
    r = await authed_client.get("/me/feed")
    assert r.status_code == 200
    body = r.json()
    assert body == {"items": [], "next_cursor": None}


# ---------------------------------------------------------------------------
# Visibility / connection filtering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_feed_returns_only_accepted_connection_events(authed_client, make_user, session):
    me = authed_client.user  # type: ignore[attr-defined]
    friend = await make_user(email="ff@example.com", display_name="F")
    stranger = await make_user(email="strang@example.com", display_name="S")
    pending = await make_user(email="pp@example.com", display_name="P")
    await _accept_pair(session, me, friend)
    # pending request only (not accepted)
    await connection_service.send_request(session, requester_id=me.id, addressee_id=pending.id)

    show = await _seed_show(session, show_id=970110)
    now = datetime.now(UTC)
    session.add_all(
        [
            _make_event(
                actor_id=friend.id,
                verb="added_show",
                target_type="show",
                target_id=show.id,
                created_at=now,
            ),
            _make_event(
                actor_id=stranger.id,
                verb="added_show",
                target_type="show",
                target_id=show.id,
                created_at=now,
            ),
            _make_event(
                actor_id=pending.id,
                verb="added_show",
                target_type="show",
                target_id=show.id,
                created_at=now,
            ),
        ]
    )
    await session.commit()

    r = await authed_client.get("/me/feed")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["actor"]["display_name"] == "F"
    assert items[0]["kind"] == "added_show"
    assert items[0]["show"]["id"] == show.id


# ---------------------------------------------------------------------------
# Ordering + pagination
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_feed_reverse_chronological_and_paginates(authed_client, make_user, session):
    me = authed_client.user  # type: ignore[attr-defined]
    friend = await make_user(email="g@example.com", display_name="G")
    await _accept_pair(session, me, friend)
    show = await _seed_show(session, show_id=970120)

    now = datetime.now(UTC)
    # 3 distinct "added_show" events (use different target_ids since uniqueness)
    show2 = await _seed_show(session, show_id=970121)
    show3 = await _seed_show(session, show_id=970122)
    events = [
        _make_event(
            actor_id=friend.id,
            verb="added_show",
            target_type="show",
            target_id=show.id,
            created_at=now - timedelta(hours=3),
        ),
        _make_event(
            actor_id=friend.id,
            verb="added_show",
            target_type="show",
            target_id=show2.id,
            created_at=now - timedelta(hours=2),
        ),
        _make_event(
            actor_id=friend.id,
            verb="added_show",
            target_type="show",
            target_id=show3.id,
            created_at=now - timedelta(hours=1),
        ),
    ]
    session.add_all(events)
    await session.commit()

    r1 = await authed_client.get("/me/feed", params={"limit": 2})
    body1 = r1.json()
    assert [i["show"]["id"] for i in body1["items"]] == [show3.id, show2.id]
    assert body1["next_cursor"] is not None

    r2 = await authed_client.get("/me/feed", params={"limit": 2, "cursor": body1["next_cursor"]})
    body2 = r2.json()
    assert [i["show"]["id"] for i in body2["items"]] == [show.id]
    assert body2["next_cursor"] is None


# ---------------------------------------------------------------------------
# Read-time rollup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rollup_folds_consecutive_same_show_episodes(authed_client, make_user, session):
    me = authed_client.user  # type: ignore[attr-defined]
    friend = await make_user(email="r1@example.com", display_name="R")
    await _accept_pair(session, me, friend)
    show = await _seed_show(session, show_id=970130, episodes_per_season=(5,))

    base = datetime.now(UTC) - timedelta(hours=1)
    # 3 episode watches within window
    for i in range(3):
        ep_id = show.id * 100 + 10 + (i + 1)
        session.add(
            _make_event(
                actor_id=friend.id,
                verb="watched_episode",
                target_type="episode",
                target_id=ep_id,
                created_at=base + timedelta(minutes=5 * i),
            )
        )
    await session.commit()

    r = await authed_client.get("/me/feed")
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["kind"] == "watched_episode_run"
    assert items[0]["rollup_count"] == 3
    assert items[0]["show"]["id"] == show.id
    assert items[0]["episode"] is None


@pytest.mark.asyncio
async def test_rollup_breaks_across_gap_window(authed_client, make_user, session):
    me = authed_client.user  # type: ignore[attr-defined]
    friend = await make_user(email="r2@example.com", display_name="R")
    await _accept_pair(session, me, friend)
    show = await _seed_show(session, show_id=970140, episodes_per_season=(4,))

    base = datetime.now(UTC) - timedelta(hours=2)
    # ep1 + ep2 within 10 min, then ep3 + ep4 after a 60-min gap.
    times = [
        base,
        base + timedelta(minutes=10),
        base + timedelta(minutes=70),
        base + timedelta(minutes=80),
    ]
    for i, t in enumerate(times):
        ep_id = show.id * 100 + 10 + (i + 1)
        session.add(
            _make_event(
                actor_id=friend.id,
                verb="watched_episode",
                target_type="episode",
                target_id=ep_id,
                created_at=t,
            )
        )
    await session.commit()

    r = await authed_client.get("/me/feed")
    items = r.json()["items"]
    # Two runs: each fold of 2 episodes
    kinds = [i["kind"] for i in items]
    counts = [i.get("rollup_count") for i in items]
    assert kinds == ["watched_episode_run", "watched_episode_run"]
    assert counts == [2, 2]


@pytest.mark.asyncio
async def test_rollup_breaks_across_shows(authed_client, make_user, session):
    me = authed_client.user  # type: ignore[attr-defined]
    friend = await make_user(email="r3@example.com", display_name="R")
    await _accept_pair(session, me, friend)
    show_a = await _seed_show(session, show_id=970150)
    show_b = await _seed_show(session, show_id=970151)

    base = datetime.now(UTC) - timedelta(hours=1)
    session.add(
        _make_event(
            actor_id=friend.id,
            verb="watched_episode",
            target_type="episode",
            target_id=show_a.id * 100 + 11,
            created_at=base,
        )
    )
    session.add(
        _make_event(
            actor_id=friend.id,
            verb="watched_episode",
            target_type="episode",
            target_id=show_b.id * 100 + 11,
            created_at=base + timedelta(minutes=5),
        )
    )
    await session.commit()

    r = await authed_client.get("/me/feed")
    items = r.json()["items"]
    assert {(i["kind"], i["show"]["id"]) for i in items} == {
        ("watched_episode", show_a.id),
        ("watched_episode", show_b.id),
    }


@pytest.mark.asyncio
async def test_rollup_breaks_across_actors(authed_client, make_user, session):
    me = authed_client.user  # type: ignore[attr-defined]
    a = await make_user(email="aa@example.com", display_name="A")
    b = await make_user(email="bb@example.com", display_name="B")
    await _accept_pair(session, me, a)
    await _accept_pair(session, me, b)
    show = await _seed_show(session, show_id=970160)

    base = datetime.now(UTC) - timedelta(hours=1)
    session.add(
        _make_event(
            actor_id=a.id,
            verb="watched_episode",
            target_type="episode",
            target_id=show.id * 100 + 11,
            created_at=base,
        )
    )
    session.add(
        _make_event(
            actor_id=b.id,
            verb="watched_episode",
            target_type="episode",
            target_id=show.id * 100 + 12,
            created_at=base + timedelta(minutes=5),
        )
    )
    await session.commit()

    r = await authed_client.get("/me/feed")
    items = r.json()["items"]
    assert len(items) == 2
    assert {i["actor"]["display_name"] for i in items} == {"A", "B"}
    assert all(i["kind"] == "watched_episode" for i in items)


@pytest.mark.asyncio
async def test_other_verbs_pass_through_unrolled(authed_client, make_user, session):
    me = authed_client.user  # type: ignore[attr-defined]
    friend = await make_user(email="pt@example.com", display_name="PT")
    await _accept_pair(session, me, friend)
    show = await _seed_show(session, show_id=970170, episodes_per_season=(1,))
    ep_id = show.id * 100 + 11
    base = datetime.now(UTC) - timedelta(hours=1)

    session.add_all(
        [
            _make_event(
                actor_id=friend.id,
                verb="added_show",
                target_type="show",
                target_id=show.id,
                created_at=base,
            ),
            _make_event(
                actor_id=friend.id,
                verb="watched_season",
                target_type="show",
                target_id=show.id,
                season_number=1,
                created_at=base + timedelta(minutes=1),
            ),
            _make_event(
                actor_id=friend.id,
                verb="watched_show",
                target_type="show",
                target_id=show.id,
                created_at=base + timedelta(minutes=2),
            ),
            _make_event(
                actor_id=friend.id,
                verb="rated_show",
                target_type="show",
                target_id=show.id,
                payload={"stars": 4.5},
                created_at=base + timedelta(minutes=3),
            ),
            _make_event(
                actor_id=friend.id,
                verb="rated_episode",
                target_type="episode",
                target_id=ep_id,
                payload={"stars": 5.0},
                created_at=base + timedelta(minutes=4),
            ),
        ]
    )
    await session.commit()

    r = await authed_client.get("/me/feed")
    items = r.json()["items"]
    # 5 distinct items, all pass-through (no rollup applies)
    by_kind = {i["kind"]: i for i in items}
    assert set(by_kind) == {
        "added_show",
        "watched_season",
        "watched_show",
        "rated_show",
        "rated_episode",
    }
    assert by_kind["watched_season"]["season_number"] == 1
    assert by_kind["rated_show"]["stars"] == 4.5
    assert by_kind["rated_episode"]["stars"] == 5.0
    assert by_kind["rated_episode"]["episode"]["id"] == ep_id
    assert by_kind["rated_episode"]["show"]["id"] == show.id


@pytest.mark.asyncio
async def test_feed_blocked_user_excluded(authed_client, make_user, session):
    me = authed_client.user  # type: ignore[attr-defined]
    blocked = await make_user(email="bk@example.com", display_name="BK")
    # block before any accepted state
    await connection_service.block(session, blocker_id=me.id, blocked_id=blocked.id)
    show = await _seed_show(session, show_id=970180)
    session.add(
        _make_event(
            actor_id=blocked.id,
            verb="added_show",
            target_type="show",
            target_id=show.id,
            created_at=datetime.now(UTC),
        )
    )
    await session.commit()

    r = await authed_client.get("/me/feed")
    assert r.json()["items"] == []


@pytest.mark.asyncio
async def test_feed_cursor_round_trip_is_stable(authed_client, make_user, session):
    me = authed_client.user  # type: ignore[attr-defined]
    friend = await make_user(email="rt@example.com", display_name="RT")
    await _accept_pair(session, me, friend)
    show = await _seed_show(session, show_id=970190)
    now = datetime.now(UTC)
    ev = _make_event(
        actor_id=friend.id,
        verb="added_show",
        target_type="show",
        target_id=show.id,
        created_at=now,
    )
    session.add(ev)
    await session.commit()

    # encoded cursor decodes back to (ts, id) -> next page returns rows strictly older
    cur = encode_cursor(now, ev.id)
    r = await authed_client.get("/me/feed", params={"cursor": cur})
    assert r.status_code == 200
    assert r.json()["items"] == []
