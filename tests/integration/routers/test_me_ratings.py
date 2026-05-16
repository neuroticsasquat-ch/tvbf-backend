"""Route tests for /me/shows/{id}/rating and /me/episodes/{id}/rating (NEU-166)."""

import pytest

from tvbf.tvmaze.models import Episode, Show


async def _seed_show(session, *, show_id: int = 8810001) -> Show:
    show = Show(id=show_id, name="ShowR", tvmaze_updated=1, status="Ended")
    session.add(show)
    await session.flush()
    return show


async def _seed_episode(session, *, show_id: int, episode_id: int) -> Episode:
    ep = Episode(id=episode_id, show_id=show_id, season=1, number=1)
    session.add(ep)
    await session.flush()
    return ep


# ---------------------------------------------------------------------------
# Show rating endpoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_show_rating_success(authed_client, session):
    show = await _seed_show(session, show_id=8810101)
    await session.commit()

    r = await authed_client.put(f"/me/shows/{show.id}/rating", json={"stars": "4.5"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["show_id"] == show.id
    assert body["stars"] == 4.5
    assert "rated_at" in body


@pytest.mark.asyncio
async def test_put_show_rating_upserts(authed_client, session):
    show = await _seed_show(session, show_id=8810102)
    await session.commit()

    r1 = await authed_client.put(f"/me/shows/{show.id}/rating", json={"stars": "2.0"})
    assert r1.status_code == 200
    r2 = await authed_client.put(f"/me/shows/{show.id}/rating", json={"stars": "3.5"})
    assert r2.status_code == 200
    assert r2.json()["stars"] == 3.5


@pytest.mark.asyncio
@pytest.mark.parametrize("stars", ["1.3", "0.0", "6.0", "-1.0"])
async def test_put_show_rating_422_invalid_stars(authed_client, session, stars):
    show = await _seed_show(session, show_id=8810103)
    await session.commit()
    r = await authed_client.put(f"/me/shows/{show.id}/rating", json={"stars": stars})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_put_show_rating_404_unknown_show(authed_client):
    r = await authed_client.put("/me/shows/999999999/rating", json={"stars": "3.0"})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_delete_show_rating_204_and_idempotent(authed_client, session):
    show = await _seed_show(session, show_id=8810104)
    await session.commit()
    await authed_client.put(f"/me/shows/{show.id}/rating", json={"stars": "3.0"})

    r1 = await authed_client.delete(f"/me/shows/{show.id}/rating")
    assert r1.status_code == 204
    r2 = await authed_client.delete(f"/me/shows/{show.id}/rating")
    assert r2.status_code == 204


@pytest.mark.asyncio
async def test_put_show_rating_missing_csrf(authed_client, session):
    show = await _seed_show(session, show_id=8810105)
    await session.commit()
    r = await authed_client.put(
        f"/me/shows/{show.id}/rating",
        json={"stars": "3.0"},
        headers={"X-CSRF-Token": ""},
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_delete_show_rating_missing_csrf(authed_client, session):
    show = await _seed_show(session, show_id=8810106)
    await session.commit()
    r = await authed_client.delete(
        f"/me/shows/{show.id}/rating",
        headers={"X-CSRF-Token": ""},
    )
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Episode rating endpoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_episode_rating_success(authed_client, session):
    show = await _seed_show(session, show_id=8810201)
    ep = await _seed_episode(session, show_id=show.id, episode_id=88102011)
    await session.commit()
    r = await authed_client.put(f"/me/episodes/{ep.id}/rating", json={"stars": "4.0"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["episode_id"] == ep.id
    assert body["stars"] == 4.0


@pytest.mark.asyncio
async def test_put_episode_rating_upserts(authed_client, session):
    show = await _seed_show(session, show_id=8810202)
    ep = await _seed_episode(session, show_id=show.id, episode_id=88102021)
    await session.commit()
    r1 = await authed_client.put(f"/me/episodes/{ep.id}/rating", json={"stars": "2.0"})
    assert r1.status_code == 200
    r2 = await authed_client.put(f"/me/episodes/{ep.id}/rating", json={"stars": "3.5"})
    assert r2.status_code == 200
    assert r2.json()["stars"] == 3.5


@pytest.mark.asyncio
@pytest.mark.parametrize("stars", ["1.3", "0.0", "6.0"])
async def test_put_episode_rating_422_invalid_stars(authed_client, session, stars):
    show = await _seed_show(session, show_id=8810203)
    ep = await _seed_episode(session, show_id=show.id, episode_id=88102031)
    await session.commit()
    r = await authed_client.put(f"/me/episodes/{ep.id}/rating", json={"stars": stars})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_put_episode_rating_404_unknown_episode(authed_client):
    r = await authed_client.put("/me/episodes/999999999/rating", json={"stars": "3.0"})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_delete_episode_rating_204_and_idempotent(authed_client, session):
    show = await _seed_show(session, show_id=8810204)
    ep = await _seed_episode(session, show_id=show.id, episode_id=88102041)
    await session.commit()
    await authed_client.put(f"/me/episodes/{ep.id}/rating", json={"stars": "3.0"})

    r1 = await authed_client.delete(f"/me/episodes/{ep.id}/rating")
    assert r1.status_code == 204
    r2 = await authed_client.delete(f"/me/episodes/{ep.id}/rating")
    assert r2.status_code == 204


@pytest.mark.asyncio
async def test_put_episode_rating_missing_csrf(authed_client, session):
    show = await _seed_show(session, show_id=8810205)
    ep = await _seed_episode(session, show_id=show.id, episode_id=88102051)
    await session.commit()
    r = await authed_client.put(
        f"/me/episodes/{ep.id}/rating",
        json={"stars": "3.0"},
        headers={"X-CSRF-Token": ""},
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_delete_episode_rating_missing_csrf(authed_client, session):
    show = await _seed_show(session, show_id=8810206)
    ep = await _seed_episode(session, show_id=show.id, episode_id=88102061)
    await session.commit()
    r = await authed_client.delete(
        f"/me/episodes/{ep.id}/rating",
        headers={"X-CSRF-Token": ""},
    )
    assert r.status_code == 403
