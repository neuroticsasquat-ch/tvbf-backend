"""Integration tests for the show cast/crew browse routes (NEU-940)."""

import httpx
import pytest
from httpx import ASGITransport

from tests.fixtures.browse.seed import seed
from tvbf.main import app
from tvbf.tvmaze import models as m


@pytest.fixture
async def client(authed_client, session):
    """Authed ASGI client with the browse seed loaded."""
    await seed(session)
    yield authed_client


@pytest.fixture
async def seeded_credits(client, session):
    """Credits for show 1. Show 2 is left bare — 27% of the catalog has none.

    Rows are inserted out of billing order so the tests prove the route sorts
    by `sort_order` rather than falling back on insertion or id order.
    """
    session.add_all(
        [
            m.Person(id=10, name="Third", tvmaze_updated=1),
            m.Person(id=11, name="Lead", tvmaze_updated=1),
            m.Person(id=12, name="Second", tvmaze_updated=1),
            m.Character(id=20, name="Sidekick", image_medium="http://img/sidekick.jpg"),
            m.Character(id=21, name="Hero"),
            m.Character(id=22, name="Villain"),
            m.CrewRole(id=30, name="Executive Producer"),
            m.CrewRole(id=31, name="Creator"),
        ]
    )
    await session.flush()
    session.add_all(
        [
            m.ShowCast(show_id=1, person_id=10, character_id=20, sort_order=2),
            m.ShowCast(
                show_id=1, person_id=11, character_id=21, sort_order=0, is_self=True, is_voice=True
            ),
            m.ShowCast(show_id=1, person_id=12, character_id=22, sort_order=1),
            m.ShowCrew(show_id=1, person_id=10, role_id=30, sort_order=1),
            m.ShowCrew(show_id=1, person_id=11, role_id=31, sort_order=0),
        ]
    )
    await session.commit()
    return client


# ---------------------------------------------------------------------------
# /shows/{id}/cast
# ---------------------------------------------------------------------------


async def test_cast_returns_billing_order(seeded_credits):
    r = await seeded_credits.get("/shows/1/cast")
    assert r.status_code == 200
    assert [c["person"]["name"] for c in r.json()] == ["Lead", "Second", "Third"]


async def test_cast_entry_shape(seeded_credits):
    r = await seeded_credits.get("/shows/1/cast")
    body = r.json()
    assert body[0] == {
        "person": {"id": 11, "name": "Lead", "image_medium": None},
        "character": {"id": 21, "name": "Hero", "image_medium": None},
        "self": True,
        "voice": True,
    }
    # Defaults come through as false, and character images are carried.
    assert body[2]["self"] is False and body[2]["voice"] is False
    assert body[2]["character"]["image_medium"] == "http://img/sidekick.jpg"


async def test_show_with_no_cast_returns_empty_list_not_404(seeded_credits):
    # 27% of the catalog has zero cast. Empty is normal, not an error.
    r = await seeded_credits.get("/shows/2/cast")
    assert r.status_code == 200
    assert r.json() == []


async def test_unknown_show_404s_for_cast(client):
    r = await client.get("/shows/999999/cast")
    assert r.status_code == 404


async def test_cast_cache_header_is_private(seeded_credits):
    r = await seeded_credits.get("/shows/1/cast")
    assert r.headers["Cache-Control"] == "private, max-age=300"


async def test_cast_requires_auth():
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/shows/1/cast")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# /shows/{id}/crew
# ---------------------------------------------------------------------------


async def test_crew_returns_sort_order(seeded_credits):
    r = await seeded_credits.get("/shows/1/crew")
    assert r.status_code == 200
    assert r.json() == [
        {"person": {"id": 11, "name": "Lead", "image_medium": None}, "role": "Creator"},
        {
            "person": {"id": 10, "name": "Third", "image_medium": None},
            "role": "Executive Producer",
        },
    ]


async def test_show_with_no_crew_returns_empty_list_not_404(seeded_credits):
    r = await seeded_credits.get("/shows/2/crew")
    assert r.status_code == 200
    assert r.json() == []


async def test_unknown_show_404s_for_crew(client):
    r = await client.get("/shows/999999/crew")
    assert r.status_code == 404


async def test_crew_cache_header_is_private(seeded_credits):
    r = await seeded_credits.get("/shows/1/crew")
    assert r.headers["Cache-Control"] == "private, max-age=300"


async def test_crew_requires_auth():
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/shows/1/crew")
    assert r.status_code == 401
