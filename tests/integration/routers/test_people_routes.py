"""Integration tests for the person detail + credits browse routes (NEU-948),
including episode-crew credits (NEU-963).

Reading `catalog` since NEU-1047. Four things about a person changed with the
source and every one is a decision recorded in `catalog/models.py` or
`catalog/schemas.py`:

* **`country_code`, `country_name`, `birthday` and `deathday` are permanently
  null.** TMDB returns none of them on a credit — they live behind a per-person
  request the credits ingest deliberately does not make (audit §5).
* **`gender` is an integer upstream** and is translated back to the words TV
  Maze sent, because the SPA renders the value verbatim.
* **Images are composed from a path**, so `image_medium` / `image_original` are
  `w185` / `original` URLs off `TMDB_IMAGE_BASE_URL` rather than stored strings.
* **A character belongs to one show and carries no image**, so a guest role that
  recurs across three shows is three interned rows.
"""

from datetime import date

import httpx
import pytest
from httpx import ASGITransport
from sqlalchemy import select

from tests.fixtures.browse.seed import seed
from tvbf.catalog import models as m
from tvbf.config import get_settings
from tvbf.main import app

# Seeded people. `EVERY_KIND` holds all four credit kinds, `CREW_ONLY` one,
# `BARE` none — the three shapes the route has to keep distinct.
EVERY_KIND = 40
CREW_ONLY = 41
BARE = 42

_IMG = get_settings().tmdb_image_base_url.rstrip("/")
GORAN_MEDIUM = f"{_IMG}/w185/goran.jpg"
GORAN_ORIGINAL = f"{_IMG}/original/goran.jpg"


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
                id=EVERY_KIND,
                tmdb_id=EVERY_KIND,
                name="Goran Višnjić",
                # TMDB's integer enum: 2 is male. The API translates it back.
                gender=2,
                profile_path="/goran.jpg",
            ),
            m.Person(id=CREW_ONLY, tmdb_id=CREW_ONLY, name="Crew Only"),
            m.Person(id=BARE, tmdb_id=BARE, name="No Credits"),
            m.Character(id=50, show_id=1, name="Hero"),
            m.Character(id=51, show_id=3, name="Villain"),
            # One guest role across three shows is three rows: `catalog.character`
            # is interned per show because TMDB models a character as free text.
            m.Character(id=52, show_id=3, name="Guest Of The Week"),
            m.Character(id=53, show_id=1, name="Guest Of The Week"),
            m.Character(id=54, show_id=2, name="Guest Of The Week"),
            m.CrewRole(id=60, department="Production", job="Executive Producer"),
            m.CrewRole(id=61, department="Writing", job="Creator"),
            # One `(department, job)` vocabulary at both grains, where `tvmaze`
            # kept two disjoint lookups.
            m.CrewRole(id=65, department="Writing", job="Writer"),
            m.CrewRole(id=66, department="Directing", job="Director"),
        ]
    )
    # Guest credits order by air date, so give the target episodes real dates.
    # Episode 3011 is left null to prove nulls sort last, not first.
    for ep_id, airdate in ((1011, date(2021, 5, 1)), (2011, date(2022, 7, 4))):
        ep = (await session.execute(select(m.Episode).where(m.Episode.id == ep_id))).scalar_one()
        ep.air_date = airdate
    await session.flush()
    session.add_all(
        [
            # Show 3 premiered 2019, show 1 premiered 2020 — inserted oldest first.
            m.ShowCast(show_id=3, person_id=EVERY_KIND, character_id=51, episode_count=9),
            m.ShowCast(show_id=1, person_id=EVERY_KIND, character_id=50, episode_count=1),
            # Two crew credits on one show — routine (writer *and* director), and
            # the credit tables carry no unique constraint, so the route has to
            # order them deterministically rather than lean on insertion order.
            m.ShowCrew(show_id=2, person_id=EVERY_KIND, role_id=60, episode_count=2),
            m.ShowCrew(show_id=2, person_id=EVERY_KIND, role_id=61, episode_count=1),
            m.ShowCrew(show_id=2, person_id=CREW_ONLY, role_id=61, episode_count=1),
            # Episode 3011 has no airdate; 1011 aired 2021, 2011 aired 2022.
            m.EpisodeGuestCast(
                episode_id=3011, person_id=EVERY_KIND, character_id=52, credit_order=0
            ),
            m.EpisodeGuestCast(
                episode_id=1011, person_id=EVERY_KIND, character_id=53, credit_order=0
            ),
            m.EpisodeGuestCast(
                episode_id=2011, person_id=EVERY_KIND, character_id=54, credit_order=1
            ),
            # Writer *and* director on episode 2011: job name sequences the pair,
            # matching what /episodes/2011/crew serves.
            m.EpisodeCrew(episode_id=2011, person_id=EVERY_KIND, role_id=65),
            m.EpisodeCrew(episode_id=2011, person_id=EVERY_KIND, role_id=66),
            m.EpisodeCrew(episode_id=3011, person_id=EVERY_KIND, role_id=66),
            m.EpisodeCrew(episode_id=1011, person_id=EVERY_KIND, role_id=66),
        ]
    )
    await session.commit()
    return client


# ---------------------------------------------------------------------------
# /people/{id}
# ---------------------------------------------------------------------------


async def test_person_detail_shape(seeded_people):
    r = await seeded_people.get(f"/people/{EVERY_KIND}")
    assert r.status_code == 200
    assert r.json() == {
        "id": EVERY_KIND,
        "name": "Goran Višnjić",
        "country_code": None,
        "country_name": None,
        "birthday": None,
        "deathday": None,
        "gender": "Male",
        "image_medium": GORAN_MEDIUM,
        "image_original": GORAN_ORIGINAL,
    }


async def test_unknown_person_404s(client):
    r = await client.get("/people/999999")
    assert r.status_code == 404


async def test_person_detail_cache_header_is_private(seeded_people):
    r = await seeded_people.get(f"/people/{EVERY_KIND}")
    assert r.headers["Cache-Control"] == "private, max-age=300"


async def test_person_detail_requires_auth():
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get(f"/people/{EVERY_KIND}")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# /people/{id}/credits
# ---------------------------------------------------------------------------


async def test_credits_returns_all_four_kinds(seeded_people):
    r = await seeded_people.get(f"/people/{EVERY_KIND}/credits")
    assert r.status_code == 200
    body = r.json()
    assert [c["show"]["id"] for c in body["cast"]] == [1, 3]  # premiere date desc
    assert [c["show"]["id"] for c in body["crew"]] == [2, 2]
    # Air date desc, nulls last: 2011 (2022), 1011 (2021), 3011 (no airdate).
    assert [g["episode"]["id"] for g in body["guest_cast"]] == [2011, 1011, 3011]
    # Same ordering for episode crew, with both roles on 2011 ahead of the rest.
    assert [c["episode"]["id"] for c in body["episode_crew"]] == [2011, 2011, 1011, 3011]


async def test_cast_credit_entry_shape(seeded_people):
    body = (await seeded_people.get(f"/people/{EVERY_KIND}/credits")).json()
    assert body["cast"][0] == {
        "show": {"id": 1, "name": "Running Drama", "image_medium": None, "premiered": "2020-01-01"},
        "character": {"id": 50, "name": "Hero", "image_medium": None},
        "self": False,
        "voice": False,
    }
    # `self`, `voice` and a character image have no TMDB counterpart, so they are
    # false/null on every entry rather than only on this one.
    assert body["cast"][1]["self"] is False and body["cast"][1]["voice"] is False


async def test_crew_credit_entry_shape(seeded_people):
    body = (await seeded_people.get(f"/people/{EVERY_KIND}/credits")).json()
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
    body = (await seeded_people.get(f"/people/{EVERY_KIND}/credits")).json()
    assert [c["role"] for c in body["crew"]] == ["Creator", "Executive Producer"]


async def test_guest_credit_carries_resolvable_show_context(seeded_people):
    body = (await seeded_people.get(f"/people/{EVERY_KIND}/credits")).json()
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
        "character": {"id": 54, "name": "Guest Of The Week", "image_medium": None},
        "self": False,
        "voice": False,
    }


async def test_episode_crew_credit_carries_resolvable_show_context(seeded_people):
    body = (await seeded_people.get(f"/people/{EVERY_KIND}/credits")).json()
    # Enough to render "Ended Drama — S1E1 · Director" without a second round
    # trip. This credit has no person-side upstream route at all (ADR-0003).
    assert body["episode_crew"][0] == {
        "show": {"id": 2, "name": "Ended Drama", "image_medium": None, "premiered": "2012-01-01"},
        "episode": {
            "id": 2011,
            "name": "Ended Drama S1E1",
            "season": 1,
            "number": 1,
            "airdate": "2022-07-04",
        },
        "role": "Director",
    }


async def test_two_episode_crew_roles_on_one_episode_keep_job_order(seeded_people):
    # Within one episode the person view has to serve the same sequence
    # /episodes/{id}/crew does, or the two views of the same episode disagree.
    # That sequence is job name now: TMDB sends no order on a crew entry.
    body = (await seeded_people.get(f"/people/{EVERY_KIND}/credits")).json()
    on_2011 = [c["role"] for c in body["episode_crew"] if c["episode"]["id"] == 2011]
    assert on_2011 == ["Director", "Writer"]


async def test_single_category_person_gets_empty_arrays_not_missing_keys(seeded_people):
    r = await seeded_people.get(f"/people/{CREW_ONLY}/credits")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"cast", "crew", "guest_cast", "episode_crew"}
    assert body["cast"] == [] and body["guest_cast"] == [] and body["episode_crew"] == []
    assert [c["role"] for c in body["crew"]] == ["Creator"]


async def test_person_with_no_credits_returns_four_empty_arrays(seeded_people):
    # Plenty of people in the mirror have no credits — that's a 200, not a 404.
    r = await seeded_people.get(f"/people/{BARE}/credits")
    assert r.status_code == 200
    assert r.json() == {"cast": [], "crew": [], "guest_cast": [], "episode_crew": []}


async def test_unknown_person_404s_for_credits(client):
    r = await client.get("/people/999999/credits")
    assert r.status_code == 404


async def test_credits_cache_header_is_private(seeded_people):
    r = await seeded_people.get(f"/people/{EVERY_KIND}/credits")
    assert r.headers["Cache-Control"] == "private, max-age=300"


async def test_credits_requires_auth():
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get(f"/people/{EVERY_KIND}/credits")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# /people?search=  (NEU-950)
# ---------------------------------------------------------------------------


async def test_search_folds_accents(seeded_people):
    r = await seeded_people.get("/people", params={"search": "visnjic"})
    assert r.status_code == 200
    body = r.json()
    assert [p["id"] for p in body["items"]] == [EVERY_KIND]
    assert body["total"] == 1


async def test_search_items_carry_full_person_detail(seeded_people):
    r = await seeded_people.get("/people", params={"search": "visnjic"})
    assert r.json()["items"][0] == {
        "id": EVERY_KIND,
        "name": "Goran Višnjić",
        "country_code": None,
        "country_name": None,
        "birthday": None,
        "deathday": None,
        "gender": "Male",
        "image_medium": GORAN_MEDIUM,
        "image_original": GORAN_ORIGINAL,
    }


async def test_search_tokens_are_anded(seeded_people):
    hit = await seeded_people.get("/people", params={"search": "goran visnjic"})
    assert [p["id"] for p in hit.json()["items"]] == [EVERY_KIND]

    miss = await seeded_people.get("/people", params={"search": "goran levi"})
    assert miss.json() == {
        "items": [],
        "page": 1,
        "per_page": 50,
        "total": 0,
        "total_pages": 1,
    }


async def test_punctuation_only_search_returns_nothing(seeded_people):
    r = await seeded_people.get("/people", params={"search": "--"})
    assert r.status_code == 200
    assert r.json()["items"] == [] and r.json()["total"] == 0


async def test_search_paginates(seeded_people):
    # "o" hits all three seeded people (Goran, Crew Only, No Credits); ask for
    # one at a time.
    params = {"search": "o", "per_page": 1}
    first = await seeded_people.get("/people", params={**params, "page": 1})
    body = first.json()
    assert body["total"] == 3
    assert body["total_pages"] == 3
    assert body["page"] == 1 and body["per_page"] == 1
    assert len(body["items"]) == 1

    second = await seeded_people.get("/people", params={**params, "page": 2})
    assert second.json()["items"][0]["id"] != body["items"][0]["id"]


async def test_search_param_is_required(seeded_people):
    # /people is a search endpoint, not a browse-all-people one.
    assert (await seeded_people.get("/people")).status_code == 422


async def test_blank_search_matches_nothing(seeded_people):
    r = await seeded_people.get("/people", params={"search": ""})
    assert r.status_code == 200
    assert r.json()["items"] == [] and r.json()["total"] == 0


async def test_search_rejects_out_of_range_pagination(seeded_people):
    base = {"search": "visnjic"}
    assert (await seeded_people.get("/people", params={**base, "page": 0})).status_code == 422
    assert (await seeded_people.get("/people", params={**base, "per_page": 101})).status_code == 422


async def test_search_cache_header_is_private(seeded_people):
    r = await seeded_people.get("/people", params={"search": "visnjic"})
    assert r.headers["Cache-Control"] == "private, max-age=300"


async def test_search_requires_auth():
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/people", params={"search": "visnjic"})
    assert r.status_code == 401
