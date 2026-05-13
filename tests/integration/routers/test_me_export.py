"""Integration tests for GET /me/export."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient

from tvbf.app.models import UserEpisodeWatch, UserShowWatch
from tvbf.main import app
from tvbf.tvmaze import models as tv


async def _seed_show(
    session,
    *,
    show_id: int,
    name: str,
    episodes: list[tuple[int, int, int]],  # (episode_id, season, number)
) -> None:
    session.add(
        tv.Show(
            id=show_id,
            name=name,
            type="Scripted",
            status="Running",
            language="English",
            tvmaze_updated=1_700_000_000 + show_id,
        )
    )
    await session.flush()
    seasons = {s for _, s, _ in episodes}
    for s in seasons:
        season_id = show_id * 100 + s
        session.add(tv.Season(id=season_id, show_id=show_id, number=s, episode_order=2))
    await session.flush()
    for episode_id, season, number in episodes:
        session.add(
            tv.Episode(
                id=episode_id,
                show_id=show_id,
                season_id=show_id * 100 + season,
                season=season,
                number=number,
                name=f"{name} S{season}E{number}",
            )
        )
    await session.flush()


@pytest.mark.asyncio
async def test_export_returns_account_my_shows_and_watch_history(authed_client, session):
    me = authed_client.user
    await _seed_show(session, show_id=101, name="Alpha", episodes=[(10101, 1, 1), (10102, 1, 2)])
    await _seed_show(session, show_id=202, name="Beta", episodes=[(20201, 1, 1)])

    # My Shows: Alpha (older), Beta (newer)
    older = datetime(2026, 1, 1, tzinfo=UTC)
    newer = datetime(2026, 2, 1, tzinfo=UTC)
    session.add(UserShowWatch(user_id=me.id, show_id=101, created_at=older))
    session.add(UserShowWatch(user_id=me.id, show_id=202, created_at=newer))

    # Watch history: two Alpha episodes + one Beta episode, deliberately
    # inserted out of chronological order to confirm the query orders by
    # watched_at.
    session.add(
        UserEpisodeWatch(
            user_id=me.id,
            episode_id=10102,
            watched_at=datetime(2026, 3, 2, tzinfo=UTC),
        )
    )
    session.add(
        UserEpisodeWatch(
            user_id=me.id,
            episode_id=10101,
            watched_at=datetime(2026, 3, 1, tzinfo=UTC),
        )
    )
    session.add(
        UserEpisodeWatch(
            user_id=me.id,
            episode_id=20201,
            watched_at=datetime(2026, 3, 3, tzinfo=UTC),
        )
    )
    await session.commit()

    r = await authed_client.get("/me/export")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert "attachment" in r.headers["content-disposition"]
    assert "tvbf-export-" in r.headers["content-disposition"]
    assert r.headers["content-disposition"].endswith('.json"')

    body = json.loads(r.text)

    # account
    assert body["account"]["id"] == str(me.id)
    assert body["account"]["email"] == me.email
    assert body["account"]["display_name"] == me.display_name
    assert body["account"]["email_verified_at"] is None

    # my_shows: ordered by added_at ascending
    assert [s["show_id"] for s in body["my_shows"]] == [101, 202]
    assert body["my_shows"][0]["show_name"] == "Alpha"
    assert body["my_shows"][1]["show_name"] == "Beta"

    # watch_history: ordered by watched_at ascending
    assert [w["episode_id"] for w in body["watch_history"]] == [10101, 10102, 20201]
    assert body["watch_history"][0] == {
        "episode_id": 10101,
        "show_id": 101,
        "season": 1,
        "number": 1,
        "watched_at": "2026-03-01T00:00:00+00:00",
    }


@pytest.mark.asyncio
async def test_export_isolates_users(authed_client, make_user, session):
    me = authed_client.user
    other = await make_user(email="other@example.com")

    await _seed_show(session, show_id=101, name="MyShow", episodes=[(10101, 1, 1)])
    await _seed_show(session, show_id=999, name="TheirShow", episodes=[(99901, 1, 1)])

    session.add(
        UserShowWatch(
            user_id=me.id,
            show_id=101,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    )
    session.add(
        UserShowWatch(
            user_id=other.id,
            show_id=999,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    )
    session.add(
        UserEpisodeWatch(
            user_id=me.id,
            episode_id=10101,
            watched_at=datetime(2026, 3, 1, tzinfo=UTC),
        )
    )
    session.add(
        UserEpisodeWatch(
            user_id=other.id,
            episode_id=99901,
            watched_at=datetime(2026, 3, 1, tzinfo=UTC),
        )
    )
    await session.commit()

    body = json.loads((await authed_client.get("/me/export")).text)
    assert [s["show_id"] for s in body["my_shows"]] == [101]
    assert [w["episode_id"] for w in body["watch_history"]] == [10101]


@pytest.mark.asyncio
async def test_export_for_user_with_no_data_returns_empty_lists(authed_client):
    body = json.loads((await authed_client.get("/me/export")).text)
    assert body["my_shows"] == []
    assert body["watch_history"] == []
    assert body["account"]["email"] == "user@example.com"


@pytest.mark.asyncio
async def test_export_requires_auth():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        r = await c.get("/me/export")
    assert r.status_code == 401
