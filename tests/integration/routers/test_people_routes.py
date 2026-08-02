"""Integration tests for the person detail + credits browse routes (NEU-948)."""

from datetime import date

import httpx
import pytest
from httpx import ASGITransport
from sqlalchemy import select

from tests.fixtures.browse.seed import seed
from tvbf.main import app
from tvbf.tvmaze import models as m

# Seeded people. `TRIPLE` has all three credit kinds, `CREW_ONLY` has one,
# `BARE` has none — the three shapes the route has to keep distinct.
TRIPLE = 40
CREW_ONLY = 41
BARE = 42


@pytest.fixture
async def client(authed_client, session):
    """Authed ASGI client with the browse seed loaded."""
    await seed(session)
    yield authed_client


@pytest.fixture
async def seeded_people(client, session):
    """People, credits, and airdates on the episodes the guest credits point at.

    Rows go in out of order throughout, so the tests prove the routes order by
    premiere/air date rather than by insertion or id.
    """
    session.add_all(
        [
            m.Person(
                id=TRIPLE,
                name="Goran Višnjić",
                country_code="HR",
                country_name="Croatia",
                birthday=date(1972, 9, 9),
                gender="Male",
                image_medium="http://img/goran-m.jpg",
                image_original="http://img/goran-o.jpg",
                tvmaze_updated=1,
            ),
            m.Person(id=CREW_ONLY, name="Crew Only", tvmaze_updated=1),
            m.Person(id=BARE, name="No Credits", tvmaze_updated=1),
            m.Character(id=50, name="Hero", image_medium="http://img/hero.jpg"),
            m.Character(id=51, name="Villain"),
            m.Character(id=52, name="Guest Of The Week"),
            m.CrewRole(id=60, name="Executive Producer"),
            m.CrewRole(id=61, name="Creator"),
        ]
    )
    # Guest credits order by air date, so give the target episodes real dates.
    # Episode 3011 is left null to prove nulls sort last, not first.
    for ep_id, airdate in ((1011, date(2021, 5, 1)), (2011, date(2022, 7, 4))):
        ep = (await session.execute(select(m.Episode).where(m.Episode.id == ep_id))).scalar_one()
        ep.airdate = airdate
    await session.flush()
    session.add_all(
        [
            # Show 3 premiered 2019, show 1 premiered 2020 — inserted oldest first.
            m.ShowCast(show_id=3, person_id=TRIPLE, character_id=51, sort_order=0),
            m.ShowCast(
                show_id=1,
                person_id=TRIPLE,
                character_id=50,
                sort_order=4,
                is_self=True,
                is_voice=True,
            ),
            # Two crew credits on one show — routine (writer *and* director), and
            # the credit tables carry no unique constraint, so the route has to
            # order them deterministically rather than lean on insertion order.
            m.ShowCrew(show_id=2, person_id=TRIPLE, role_id=60, sort_order=0),
            m.ShowCrew(show_id=2, person_id=TRIPLE, role_id=61, sort_order=1),
            m.ShowCrew(show_id=2, person_id=CREW_ONLY, role_id=61, sort_order=1),
            # Episode 3011 has no airdate; 1011 aired 2021, 2011 aired 2022.
            m.EpisodeGuestCast(episode_id=3011, person_id=TRIPLE, character_id=52, sort_order=0),
            m.EpisodeGuestCast(episode_id=1011, person_id=TRIPLE, character_id=52, sort_order=0),
            m.EpisodeGuestCast(episode_id=2011, person_id=TRIPLE, character_id=52, sort_order=1),
        ]
    )
    await session.commit()
    return client


# ---------------------------------------------------------------------------
# /people/{id}
# ---------------------------------------------------------------------------


async def test_person_detail_shape(seeded_people):
    r = await seeded_people.get(f"/people/{TRIPLE}")
    assert r.status_code == 200
    assert r.json() == {
        "id": TRIPLE,
        "name": "Goran Višnjić",
        "country_code": "HR",
        "country_name": "Croatia",
        "birthday": "1972-09-09",
        "deathday": None,
        "gender": "Male",
        "image_medium": "http://img/goran-m.jpg",
        "image_original": "http://img/goran-o.jpg",
    }


async def test_unknown_person_404s(client):
    r = await client.get("/people/999999")
    assert r.status_code == 404


async def test_person_detail_cache_header_is_private(seeded_people):
    r = await seeded_people.get(f"/people/{TRIPLE}")
    assert r.headers["Cache-Control"] == "private, max-age=300"


async def test_person_detail_requires_auth():
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get(f"/people/{TRIPLE}")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# /people/{id}/credits
# ---------------------------------------------------------------------------


async def test_credits_returns_all_three_kinds(seeded_people):
    r = await seeded_people.get(f"/people/{TRIPLE}/credits")
    assert r.status_code == 200
    body = r.json()
    assert [c["show"]["id"] for c in body["cast"]] == [1, 3]  # premiere date desc
    assert [c["show"]["id"] for c in body["crew"]] == [2, 2]
    # Air date desc, nulls last: 2011 (2022), 1011 (2021), 3011 (no airdate).
    assert [g["episode"]["id"] for g in body["guest_cast"]] == [2011, 1011, 3011]


async def test_cast_credit_entry_shape(seeded_people):
    body = (await seeded_people.get(f"/people/{TRIPLE}/credits")).json()
    assert body["cast"][0] == {
        "show": {"id": 1, "name": "Running Drama", "image_medium": None, "premiered": "2020-01-01"},
        "character": {"id": 50, "name": "Hero", "image_medium": "http://img/hero.jpg"},
        "self": True,
        "voice": True,
    }
    assert body["cast"][1]["self"] is False and body["cast"][1]["voice"] is False


async def test_crew_credit_entry_shape(seeded_people):
    body = (await seeded_people.get(f"/people/{TRIPLE}/credits")).json()
    assert body["crew"][0] == {
        "show": {
            "id": 2,
            "name": "Ended Drama",
            "image_medium": None,
            "premiered": "2012-01-01",
        },
        "role": "Creator",
    }


async def test_multiple_credits_on_one_show_order_deterministically(seeded_people):
    # No unique constraint on the credit tables, so two credits can share a show.
    # Role name breaks the tie; without it the order is whatever Postgres returns.
    body = (await seeded_people.get(f"/people/{TRIPLE}/credits")).json()
    assert [c["role"] for c in body["crew"]] == ["Creator", "Executive Producer"]


async def test_guest_credit_carries_resolvable_show_context(seeded_people):
    body = (await seeded_people.get(f"/people/{TRIPLE}/credits")).json()
    # Enough to render "Ended Drama — S1E1" without a second round trip.
    assert body["guest_cast"][0] == {
        "show": {"id": 2, "name": "Ended Drama", "image_medium": None, "premiered": "2012-01-01"},
        "episode": {
            "id": 2011,
            "name": "Ended Drama S1E1",
            "season": 1,
            "number": 1,
            "airdate": "2022-07-04",
        },
        "character": {"id": 52, "name": "Guest Of The Week", "image_medium": None},
        "self": False,
        "voice": False,
    }


async def test_single_category_person_gets_empty_arrays_not_missing_keys(seeded_people):
    r = await seeded_people.get(f"/people/{CREW_ONLY}/credits")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"cast", "crew", "guest_cast"}
    assert body["cast"] == [] and body["guest_cast"] == []
    assert [c["role"] for c in body["crew"]] == ["Creator"]


async def test_person_with_no_credits_returns_three_empty_arrays(seeded_people):
    # Plenty of people in the mirror have no credits — that's a 200, not a 404.
    r = await seeded_people.get(f"/people/{BARE}/credits")
    assert r.status_code == 200
    assert r.json() == {"cast": [], "crew": [], "guest_cast": []}


async def test_unknown_person_404s_for_credits(client):
    r = await client.get("/people/999999/credits")
    assert r.status_code == 404


async def test_credits_cache_header_is_private(seeded_people):
    r = await seeded_people.get(f"/people/{TRIPLE}/credits")
    assert r.headers["Cache-Control"] == "private, max-age=300"


async def test_credits_requires_auth():
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get(f"/people/{TRIPLE}/credits")
    assert r.status_code == 401
