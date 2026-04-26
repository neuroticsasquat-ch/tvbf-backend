"""Integration tests for me routes.

Merges tests from tests/test_me_routes.py and the me handler tests from
tests/test_route_handlers.py.
"""

import pytest
from fastapi import HTTPException, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from tvbf.app.dto import AccountDeleteRequest
from tvbf.app.models import User
from tvbf.app.services import my_shows_service  # noqa: F401 — used implicitly
from tvbf.config import get_settings
from tvbf.main import app
from tvbf.routers import me as me_router
from tvbf.tvmaze.models import Episode, Show

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


async def _seed_show(session, *, show_id: int, name: str = "S", episodes: int = 2) -> Show:
    show = Show(id=show_id, name=name, tvmaze_updated=1)
    session.add(show)
    await session.flush()
    for i in range(1, episodes + 1):
        session.add(Episode(id=show_id * 100 + i, show_id=show.id, season=1, number=i))
    await session.flush()
    return show


async def _seed_show_with_seasons(session, *, show_id: int, seasons: dict[int, int]) -> Show:
    show = Show(id=show_id, name=f"Show{show_id}", tvmaze_updated=1)
    session.add(show)
    await session.flush()
    ep_id = show_id * 1000
    for season, count in seasons.items():
        for n in range(1, count + 1):
            ep_id += 1
            session.add(Episode(id=ep_id, show_id=show.id, season=season, number=n))
    await session.flush()
    return show


def _request(*, cookies: dict[str, str] | None = None) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if cookies:
        headers.append((b"cookie", "; ".join(f"{k}={v}" for k, v in cookies.items()).encode()))
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": headers,
        "query_string": b"",
        "client": ("127.0.0.1", 12345),
    }
    return Request(scope)


# ---------------------------------------------------------------------------
# Route-level tests (via ASGITransport) — from test_me_routes.py
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_me_returns_current_user(authed_client):
    r = await authed_client.get("/me")
    assert r.status_code == 200
    assert r.json()["email"] == "user@example.com"


@pytest.mark.asyncio
async def test_me_returns_401_when_no_session():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        r = await c.get("/me")
    assert r.status_code == 401
    assert r.json()["detail"] == "auth_required"


@pytest.mark.asyncio
async def test_delete_me_requires_password(authed_client):
    r = await authed_client.request(
        "DELETE",
        "/me",
        json={"password": "wrong"},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_delete_me_succeeds_and_cascades(authed_client, session):
    user_id = authed_client.user.id  # type: ignore[attr-defined]
    r = await authed_client.request(
        "DELETE",
        "/me",
        json={"password": "hunter2hunter2"},
    )
    assert r.status_code == 204
    found = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    assert found is None


@pytest.mark.asyncio
async def test_delete_me_requires_csrf(session, make_user):
    from tvbf.app import tokens
    from tvbf.app.repos import session_repo

    user = await make_user(email="nocsrf@example.com")
    sess_id = tokens.new_session_id()
    await session_repo.create(
        session, session_id=sess_id, user_id=user.id, ttl_days=30, user_agent=None, ip=None
    )
    await session.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        c.cookies.set("tvbf_session", sess_id, domain="test")
        r = await c.request("DELETE", "/me", json={"password": "hunter2hunter2"})
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_put_show_adds_to_my_shows(authed_client, session):
    show = await _seed_show(session, show_id=920001)
    await session.commit()

    r = await authed_client.put(f"/me/shows/{show.id}")
    assert r.status_code == 204

    r = await authed_client.get("/me/shows")
    body = r.json()
    assert len(body) == 1
    assert body[0]["show"]["id"] == show.id


@pytest.mark.asyncio
async def test_put_show_idempotent(authed_client, session):
    show = await _seed_show(session, show_id=920002)
    await session.commit()
    r1 = await authed_client.put(f"/me/shows/{show.id}")
    r2 = await authed_client.put(f"/me/shows/{show.id}")
    assert r1.status_code == 204
    assert r2.status_code == 204
    r3 = await authed_client.get("/me/shows")
    assert len(r3.json()) == 1


@pytest.mark.asyncio
async def test_put_show_404_for_unknown_show(authed_client):
    r = await authed_client.put("/me/shows/999999999")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_get_my_shows_returns_tracked_only(authed_client, session):
    a = await _seed_show(session, show_id=920004, name="A")
    b = await _seed_show(session, show_id=920005, name="B")
    await session.commit()
    await authed_client.put(f"/me/shows/{a.id}")

    r = await authed_client.get("/me/shows")
    body = r.json()
    ids = [e["show"]["id"] for e in body]
    assert a.id in ids
    assert b.id not in ids


@pytest.mark.asyncio
async def test_delete_show_preserves_episode_history(authed_client, session):
    show = await _seed_show(session, show_id=920006)
    await session.commit()
    await authed_client.put(f"/me/shows/{show.id}")
    await authed_client.post(f"/me/episodes/{show.id * 100 + 1}/watched")

    r = await authed_client.request("DELETE", f"/me/shows/{show.id}")
    assert r.status_code == 204

    r = await authed_client.get("/me/shows")
    assert r.json() == []

    await authed_client.put(f"/me/shows/{show.id}")
    r = await authed_client.get("/me/shows")
    body = r.json()
    assert body[0]["watched_episode_count"] == 1


@pytest.mark.asyncio
async def test_mark_episode_watched(authed_client, session):
    await _seed_show(session, show_id=921001)
    await session.commit()
    ep_id = 921001 * 100 + 1
    r = await authed_client.post(f"/me/episodes/{ep_id}/watched")
    assert r.status_code == 201
    assert r.json()["episode_id"] == ep_id


@pytest.mark.asyncio
async def test_mark_episode_watched_idempotent(authed_client, session):
    await _seed_show(session, show_id=921002)
    await session.commit()
    ep_id = 921002 * 100 + 1
    await authed_client.post(f"/me/episodes/{ep_id}/watched")
    r = await authed_client.post(f"/me/episodes/{ep_id}/watched")
    assert r.status_code == 201


@pytest.mark.asyncio
async def test_unmark_episode_watched(authed_client, session):
    await _seed_show(session, show_id=921003)
    await session.commit()
    ep_id = 921003 * 100 + 1
    await authed_client.post(f"/me/episodes/{ep_id}/watched")
    r = await authed_client.request("DELETE", f"/me/episodes/{ep_id}/watched")
    assert r.status_code == 204


@pytest.mark.asyncio
async def test_mark_unknown_episode_404(authed_client):
    r = await authed_client.post("/me/episodes/999999999/watched")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_bulk_mark_season(authed_client, session):
    show = await _seed_show_with_seasons(session, show_id=922001, seasons={1: 3, 2: 2})
    await session.commit()
    r = await authed_client.post(f"/me/shows/{show.id}/season/1/watched")
    assert r.status_code == 201
    assert r.json()["marked"] == 3

    list_r = await authed_client.get("/me/shows")
    assert list_r.json() == []

    await authed_client.put(f"/me/shows/{show.id}")
    list_r = await authed_client.get("/me/shows")
    body = list_r.json()
    assert len(body) == 1
    assert body[0]["watched_episode_count"] == 3
    assert body[0]["total_episode_count"] == 5


@pytest.mark.asyncio
async def test_bulk_unmark_season(authed_client, session):
    show = await _seed_show_with_seasons(session, show_id=922002, seasons={1: 3})
    await session.commit()
    await authed_client.post(f"/me/shows/{show.id}/season/1/watched")
    r = await authed_client.request("DELETE", f"/me/shows/{show.id}/season/1/watched")
    assert r.status_code == 204


@pytest.mark.asyncio
async def test_bulk_mark_404_for_unknown_season(authed_client, session):
    show = await _seed_show_with_seasons(session, show_id=922003, seasons={1: 1})
    await session.commit()
    r = await authed_client.post(f"/me/shows/{show.id}/season/99/watched")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Direct route handler tests (from test_route_handlers.py)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_me_route_raises_401_on_wrong_password(session, make_user):
    user = await make_user(email="dm-rt@example.com", password="hunter2hunter2")
    payload = AccountDeleteRequest(password="wrong")
    with pytest.raises(HTTPException) as ei:
        await me_router.delete_me(payload, user=user, db=session)
    assert ei.value.status_code == 401


@pytest.mark.asyncio
async def test_delete_me_route_succeeds_with_correct_password(session, make_user):
    user = await make_user(email="dm-ok-rt@example.com", password="hunter2hunter2")
    payload = AccountDeleteRequest(password="hunter2hunter2")
    out = await me_router.delete_me(payload, user=user, db=session)
    assert out.status_code == 204


@pytest.mark.asyncio
async def test_add_show_route_raises_404_for_unknown_show(session, make_user):
    user = await make_user()
    with pytest.raises(HTTPException) as ei:
        await me_router.add_show_route(show_id=999_999_999, user=user, db=session)
    assert ei.value.status_code == 404


@pytest.mark.asyncio
async def test_mark_episode_route_raises_404_for_unknown_episode(session, make_user):
    user = await make_user()
    with pytest.raises(HTTPException) as ei:
        await me_router.mark_episode_watched(episode_id=999_999_999, user=user, db=session)
    assert ei.value.status_code == 404


@pytest.mark.asyncio
async def test_bulk_mark_season_route_raises_404_for_unknown_season(session, make_user):
    user = await make_user()
    with pytest.raises(HTTPException) as ei:
        await me_router.bulk_mark_season(show_id=999_999, season_number=99, user=user, db=session)
    assert ei.value.status_code == 404


@pytest.mark.asyncio
async def test_me_route_returns_authed_user(session, make_user):
    user = await make_user(email="me-rt@example.com")
    settings = get_settings()
    request = _request(cookies={"csrf_token": "ignored-here"})
    out = await me_router.me(request, user=user, settings=settings)
    assert out.email == "me-rt@example.com"


@pytest.mark.asyncio
async def test_list_my_shows_route_returns_list(session, make_user):
    user = await make_user(email="list-rt@example.com")
    out = await me_router.list_my_shows_route(sort="recent_activity", user=user, db=session)
    assert out == []


@pytest.mark.asyncio
async def test_add_show_route_returns_204_for_known_show(session, make_user):
    user = await make_user()
    show = await _seed_show(session, show_id=940001)
    await session.commit()
    out = await me_router.add_show_route(show_id=show.id, user=user, db=session)
    assert out.status_code == 204


@pytest.mark.asyncio
async def test_remove_show_route_returns_204(session, make_user):
    user = await make_user()
    show = await _seed_show(session, show_id=940002)
    await session.commit()
    await me_router.add_show_route(show_id=show.id, user=user, db=session)
    out = await me_router.remove_show_route(show_id=show.id, user=user, db=session)
    assert out.status_code == 204


@pytest.mark.asyncio
async def test_watch_next_route_returns_list(session, make_user):
    user = await make_user(email="wn-rt@example.com")
    out = await me_router.watch_next_route(sort="airdate_desc", user=user, db=session)
    assert out == []


@pytest.mark.asyncio
async def test_upcoming_route_returns_list(session, make_user):
    user = await make_user(email="up-rt@example.com")
    out = await me_router.upcoming_route(sort="airdate_asc", user=user, db=session)
    assert out == []


@pytest.mark.asyncio
async def test_list_watched_episodes_route_returns_list(session, make_user):
    user = await make_user(email="wep-rt@example.com")
    show = await _seed_show(session, show_id=940003)
    await session.commit()
    out = await me_router.list_watched_episodes_for_show(show_id=show.id, user=user, db=session)
    assert out == []


@pytest.mark.asyncio
async def test_mark_episode_route_returns_episode_watch_out(session, make_user):
    user = await make_user()
    show = await _seed_show(session, show_id=940004)
    await session.commit()
    ep_id = show.id * 100 + 1
    out = await me_router.mark_episode_watched(episode_id=ep_id, user=user, db=session)
    assert out.episode_id == ep_id


@pytest.mark.asyncio
async def test_unmark_episode_route_returns_204(session, make_user):
    user = await make_user()
    show = await _seed_show(session, show_id=940005)
    await session.commit()
    ep_id = show.id * 100 + 1
    out = await me_router.unmark_episode_watched(episode_id=ep_id, user=user, db=session)
    assert out.status_code == 204


@pytest.mark.asyncio
async def test_bulk_mark_season_route_returns_count(session, make_user):
    user = await make_user()
    show = await _seed_show(session, show_id=940006, episodes=3)
    await session.commit()
    out = await me_router.bulk_mark_season(show_id=show.id, season_number=1, user=user, db=session)
    assert out.marked == 3


@pytest.mark.asyncio
async def test_bulk_unmark_season_route_returns_204(session, make_user):
    user = await make_user()
    show = await _seed_show(session, show_id=940007, episodes=3)
    await session.commit()
    out = await me_router.bulk_unmark_season(
        show_id=show.id, season_number=1, user=user, db=session
    )
    assert out.status_code == 204
