"""Route tests for GET /me/watched (NEU-102)."""

from datetime import UTC, date, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from tvbf.app.models import UserEpisodeWatch
from tvbf.main import app
from tvbf.tvmaze.models import Episode, Show


async def _seed(session, *, show_id: int, name: str = "S", show_status: str = "Ended"):
    show = Show(id=show_id, name=name, tvmaze_updated=1, status=show_status)
    session.add(show)
    await session.flush()
    today = date.today()
    session.add(
        Episode(
            id=show_id * 100 + 1,
            show_id=show.id,
            season=1,
            number=1,
            airdate=today - timedelta(days=1),
        )
    )
    await session.flush()
    return show


@pytest.mark.asyncio
async def test_me_watched_requires_auth():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        r = await c.get("/me/watched")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_me_watched_returns_payload(authed_client, session):
    me = authed_client.user  # type: ignore[attr-defined]
    show = await _seed(session, show_id=940200, name="Watched")
    session.add(
        UserEpisodeWatch(user_id=me.id, episode_id=show.id * 100 + 1, watched_at=datetime.now(UTC))
    )
    await session.commit()

    r = await authed_client.get("/me/watched")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    row = body[0]
    assert row["show"]["id"] == show.id
    assert row["status"] == "finished"
    assert row["watched_episode_count"] == 1
    assert row["aired_episode_count"] == 1
    assert row["in_my_shows"] is False
    assert row["last_watched_at"] is not None


@pytest.mark.asyncio
async def test_me_watched_status_filter(authed_client, session):
    me = authed_client.user  # type: ignore[attr-defined]
    finished_show = await _seed(session, show_id=940201, name="Finished")
    progress_show = Show(id=940202, name="WIP", tvmaze_updated=1, status="Ended")
    session.add(progress_show)
    await session.flush()
    today = date.today()
    for i in (1, 2):
        session.add(
            Episode(
                id=940202 * 100 + i,
                show_id=progress_show.id,
                season=1,
                number=i,
                airdate=today - timedelta(days=2 - i + 1),
            )
        )
    await session.flush()
    session.add(
        UserEpisodeWatch(
            user_id=me.id,
            episode_id=finished_show.id * 100 + 1,
            watched_at=datetime.now(UTC),
        )
    )
    session.add(
        UserEpisodeWatch(user_id=me.id, episode_id=940202 * 100 + 1, watched_at=datetime.now(UTC))
    )
    await session.commit()

    r = await authed_client.get("/me/watched", params={"status": "finished"})
    assert r.status_code == 200
    assert [row["show"]["id"] for row in r.json()] == [finished_show.id]

    r = await authed_client.get("/me/watched", params={"status": "in_progress"})
    assert r.status_code == 200
    assert [row["show"]["id"] for row in r.json()] == [940202]


@pytest.mark.asyncio
async def test_me_watched_invalid_status_rejected(authed_client):
    r = await authed_client.get("/me/watched", params={"status": "bogus"})
    assert r.status_code == 422
