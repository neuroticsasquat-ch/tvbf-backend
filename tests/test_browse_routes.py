import httpx
import pytest
from httpx import ASGITransport

from tests.fixtures.browse.seed import GENRES, seed
from tvbf.main import app


@pytest.fixture
async def client(session):
    """ASGI client whose test DB is the one `session` manages."""
    await seed(session)
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


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


# Task 11: base pagination
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


# Task 12: simple filters
async def test_get_shows_search_substring(client):
    r = await client.get("/shows?search=drama")
    body = r.json()
    names = {i["name"] for i in body["items"]}
    assert "Running Drama" in names
    assert "Running Comedy" not in names


async def test_get_shows_status_filter(client):
    r = await client.get("/shows?status=Ended")
    assert {i["name"] for i in r.json()["items"]} == {"Ancient Show", "Ended Drama"}


# Task 13: genre filter
async def test_get_shows_multi_genre(client):
    r = await client.get("/shows?genre=Drama&genre=Crime")
    assert {i["name"] for i in r.json()["items"]} == {"Multi Genre", "Running Drama"}


# Task 14: network filter
async def test_get_shows_multi_network(client):
    r = await client.get("/shows?network=1&network=2")
    names = {i["name"] for i in r.json()["items"]}
    assert "Ancient Show" not in names
    assert "Web Only" not in names
    assert "Running Drama" in names
    assert "Spanish Drama" in names


# Task 15: sort
async def test_get_shows_sort_invalid_returns_422(client):
    r = await client.get("/shows?sort=popularity")
    assert r.status_code == 422


async def test_get_shows_sort_premiered_desc(client):
    r = await client.get("/shows?sort=-premiered")
    items = r.json()["items"]
    assert items[0]["name"] == "TBD Show"
