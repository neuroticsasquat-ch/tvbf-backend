"""Integration tests for browse routes.

Merges tests from tests/test_browse_routes.py, tests/test_browse_cors_and_cache.py,
and the browse handler tests from tests/test_route_handlers_browse_admin.py.
"""

import httpx
import pytest
from fastapi import HTTPException
from httpx import ASGITransport

from tests.fixtures.browse.seed import GENRES, seed
from tvbf.main import app
from tvbf.routers import browse as browse_router


@pytest.fixture
async def client(authed_client, session):
    """Authed ASGI client with the browse seed loaded."""
    await seed(session)
    yield authed_client


@pytest.fixture
async def seeded(session):
    await seed(session)
    return session


# ---------------------------------------------------------------------------
# Route-level tests (via ASGITransport)
# ---------------------------------------------------------------------------


async def test_get_genres_returns_flat_sorted_list(client):
    r = await client.get("/genres")
    assert r.status_code == 200
    body = r.json()
    assert [g["name"] for g in body] == sorted(GENRES)
    assert all("id" in g and "name" in g for g in body)


async def test_get_networks_returns_flat_sorted_list(client):
    r = await client.get("/networks")
    assert r.status_code == 200
    body = r.json()
    assert [n["name"] for n in body] == ["Network A", "Network B"]
    assert body[0]["country_code"] == "US"
    assert body[1]["country_code"] == "GB"


async def test_get_show_detail_includes_seasons_and_genres(client):
    r = await client.get("/shows/9")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "Multi Genre"
    assert set(body["genres"]) == {"Drama", "Crime", "Mystery"}
    assert body["network"] == {"id": 2, "name": "Network B"}
    assert body["web_channel"] is None
    assert sorted(s["number"] for s in body["seasons"]) == [1, 2]
    assert body["tvmaze_updated"] == 1_700_000_009


async def test_get_show_detail_returns_404_for_unknown_id(client):
    r = await client.get("/shows/99999")
    assert r.status_code == 404
    assert r.json()["detail"] == "show not found"


async def test_get_show_seasons_endpoint_returns_seasons(client):
    r = await client.get("/shows/1/seasons")
    assert r.status_code == 200
    body = r.json()
    assert [s["number"] for s in body] == [1, 2]


async def test_get_show_seasons_endpoint_returns_404_for_unknown_show(client):
    r = await client.get("/shows/99999/seasons")
    assert r.status_code == 404


async def test_get_show_episodes_all(client):
    r = await client.get("/shows/1/episodes")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 4


async def test_get_show_episodes_filtered_by_season(client):
    r = await client.get("/shows/1/episodes?season=2")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 2
    assert all(e["season"] == 2 for e in body)


async def test_get_show_episodes_404_for_unknown_show(client):
    r = await client.get("/shows/99999/episodes")
    assert r.status_code == 404


async def test_get_shows_default_list(client):
    r = await client.get("/shows")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 10
    assert body["page"] == 1
    assert body["per_page"] == 50
    assert body["total_pages"] == 1
    assert len(body["items"]) == 10
    assert body["items"][0]["name"] == "Ancient Show"  # name asc


async def test_get_shows_pagination(client):
    r = await client.get("/shows?page=2&per_page=3")
    assert r.status_code == 200
    body = r.json()
    assert body["page"] == 2
    assert body["per_page"] == 3
    assert body["total"] == 10
    assert body["total_pages"] == 4
    assert len(body["items"]) == 3


async def test_get_shows_rejects_out_of_range_pagination(client):
    assert (await client.get("/shows?per_page=101")).status_code == 422
    assert (await client.get("/shows?page=0")).status_code == 422


async def test_get_shows_search_substring(client):
    r = await client.get("/shows?search=drama")
    body = r.json()
    names = {i["name"] for i in body["items"]}
    assert "Running Drama" in names
    assert "Running Comedy" not in names


async def test_get_shows_search_includes_matched_aka_when_only_aka_matches(authed_client, session):
    """Search response surfaces the matched AKA when the show name didn't match."""
    from sqlalchemy import insert

    from tvbf.tvmaze import models as m

    session.add(m.Show(id=501, name="東京リベンジャーズ", tvmaze_updated=1))
    await session.flush()
    await session.execute(
        insert(m.ShowAka).values(
            [{"show_id": 501, "name": "Tokyo Revengers", "country_code": "US"}]
        )
    )
    await session.commit()

    r = await authed_client.get("/shows?search=tokyo+revengers")
    body = r.json()
    items = {i["id"]: i for i in body["items"]}
    assert 501 in items
    assert items[501]["matched_aka"] == "Tokyo Revengers"


async def test_get_shows_search_omits_matched_aka_when_show_name_matches(client):
    """When the show's own name matches the search, matched_aka is null."""
    r = await client.get("/shows?search=running+drama")
    items = r.json()["items"]
    assert items, "expected at least one match"
    assert all(i["matched_aka"] is None for i in items)


async def test_get_shows_no_search_has_null_matched_aka(client):
    r = await client.get("/shows")
    items = r.json()["items"]
    assert all(i["matched_aka"] is None for i in items)


async def test_get_shows_status_filter(client):
    r = await client.get("/shows?status=Ended")
    assert {i["name"] for i in r.json()["items"]} == {"Ancient Show", "Ended Drama"}


async def test_get_shows_multi_genre(client):
    r = await client.get("/shows?genre=Drama&genre=Crime")
    assert {i["name"] for i in r.json()["items"]} == {"Multi Genre", "Running Drama"}


async def test_get_shows_multi_network(client):
    r = await client.get("/shows?network=1&network=2")
    names = {i["name"] for i in r.json()["items"]}
    assert "Ancient Show" not in names
    assert "Web Only" not in names
    assert "Running Drama" in names
    assert "Spanish Drama" in names


async def test_get_shows_sort_invalid_returns_422(client):
    r = await client.get("/shows?sort=popularity")
    assert r.status_code == 422


async def test_get_shows_sort_premiered_desc(client):
    r = await client.get("/shows?sort=-premiered")
    items = r.json()["items"]
    assert items[0]["name"] == "TBD Show"


# ---------------------------------------------------------------------------
# CORS + Cache-Control (from test_browse_cors_and_cache.py)
# ---------------------------------------------------------------------------


async def test_cors_allows_configured_origin():
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.options(
            "/healthz",
            headers={
                "Origin": "https://app.tvbf.localhost",
                "Access-Control-Request-Method": "GET",
            },
        )
    assert r.status_code in (200, 204)
    assert r.headers.get("access-control-allow-origin") == "https://app.tvbf.localhost"


async def test_cors_blocks_unknown_origin():
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.options(
            "/healthz",
            headers={
                "Origin": "https://attacker.example.com",
                "Access-Control-Request-Method": "GET",
            },
        )
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers.keys()}


async def test_browse_requires_auth():
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/genres")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Direct route handler tests (from test_route_handlers_browse_admin.py)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_genres_route(seeded):
    out = await browse_router.list_genres(session=seeded)
    assert isinstance(out, list)
    assert len(out) >= 1


@pytest.mark.asyncio
async def test_list_networks_route(seeded):
    out = await browse_router.list_networks(session=seeded)
    assert isinstance(out, list)


@pytest.mark.asyncio
async def test_list_shows_route_returns_page(seeded):
    out = await browse_router.list_shows_route(
        session=seeded,
        genre=[],
        network=[],
        page=1,
        per_page=50,
    )
    assert out.total >= 1
    assert out.page == 1


@pytest.mark.asyncio
async def test_list_shows_route_raises_422_for_invalid_sort(seeded):
    with pytest.raises(HTTPException) as ei:
        await browse_router.list_shows_route(
            session=seeded,
            genre=[],
            network=[],
            sort="bogus",
            page=1,
            per_page=50,
        )
    assert ei.value.status_code == 422


@pytest.mark.asyncio
async def test_get_show_route_returns_detail_for_known_id(seeded):
    out = await browse_router.get_show(show_id=9, session=seeded)
    assert out.id == 9
    assert out.name


@pytest.mark.asyncio
async def test_get_show_route_raises_404_for_unknown_id(seeded):
    with pytest.raises(HTTPException) as ei:
        await browse_router.get_show(show_id=99999, session=seeded)
    assert ei.value.status_code == 404


@pytest.mark.asyncio
async def test_get_show_seasons_route_returns_seasons(seeded):
    out = await browse_router.get_show_seasons_route(show_id=9, session=seeded)
    assert isinstance(out, list)


@pytest.mark.asyncio
async def test_get_show_seasons_route_raises_404_for_unknown_id(seeded):
    with pytest.raises(HTTPException) as ei:
        await browse_router.get_show_seasons_route(show_id=99999, session=seeded)
    assert ei.value.status_code == 404


@pytest.mark.asyncio
async def test_get_show_episodes_route_returns_episodes(seeded):
    out = await browse_router.get_show_episodes_route(show_id=9, season=None, session=seeded)
    assert isinstance(out, list)


@pytest.mark.asyncio
async def test_get_show_episodes_route_raises_404_for_unknown_id(seeded):
    with pytest.raises(HTTPException) as ei:
        await browse_router.get_show_episodes_route(show_id=99999, season=None, session=seeded)
    assert ei.value.status_code == 404
