"""Integration tests for the credit browse routes: show cast/crew (NEU-940),
episode guest cast (NEU-949) and episode crew (NEU-963).

Reading `catalog` since NEU-1047, which changed three things these tests assert
and each is a decision recorded in `catalog/models.py`:

* **Ordering is by `episode_count`, descending**, not by a billing/credit
  `sort_order`. TMDB sends no `order` at all on a crew entry, and
  `aggregate_credits` gives the measure `order` only ever proxied for. The
  fixtures below therefore bill by appearance count, and still insert rows out of
  order so the route is proved to sort rather than to echo insertion order.
* **`self` and `voice` are always false, and a character carries no image.**
  TMDB flags neither on a credit and models a character as free text, so
  `catalog.character` has no image column.
* **A character belongs to a show**, so the fixture names one per show.
"""

import httpx
import pytest
from httpx import ASGITransport

from tests.fixtures.browse.seed import seed
from tvbf.catalog import models as m
from tvbf.main import app


@pytest.fixture
async def client(authed_client, session):
    """Authed ASGI client with the browse seed loaded."""
    await seed(session)
    yield authed_client


@pytest.fixture
async def seeded_credits(client, session):
    """Credits for show 1. Show 2 is left bare — 27% of the catalog has none.

    Rows are inserted out of billing order so the tests prove the route sorts
    by `episode_count` rather than falling back on insertion or id order.
    """
    session.add_all(
        [
            m.Person(id=10, tmdb_id=10, name="Third"),
            m.Person(id=11, tmdb_id=11, name="Lead"),
            m.Person(id=12, tmdb_id=12, name="Second"),
            m.Character(id=20, show_id=1, name="Sidekick"),
            m.Character(id=21, show_id=1, name="Hero"),
            m.Character(id=22, show_id=1, name="Villain"),
            m.CrewRole(id=30, department="Production", job="Executive Producer"),
            m.CrewRole(id=31, department="Writing", job="Creator"),
        ]
    )
    await session.flush()
    session.add_all(
        [
            m.ShowCast(show_id=1, person_id=10, character_id=20, episode_count=1),
            m.ShowCast(show_id=1, person_id=11, character_id=21, episode_count=3),
            m.ShowCast(show_id=1, person_id=12, character_id=22, episode_count=2),
            m.ShowCrew(show_id=1, person_id=10, role_id=30, episode_count=1),
            m.ShowCrew(show_id=1, person_id=11, role_id=31, episode_count=2),
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
        "self": False,
        "voice": False,
    }
    # `self`, `voice` and a character image have no TMDB counterpart at all, so
    # they are false/null on every entry rather than only on this one.
    assert body[2]["self"] is False and body[2]["voice"] is False
    assert body[2]["character"]["image_medium"] is None


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


async def test_crew_returns_episode_count_order(seeded_credits):
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


# ---------------------------------------------------------------------------
# /episodes/{id}/guest-cast
# ---------------------------------------------------------------------------


@pytest.fixture
async def seeded_guest_cast(client, session):
    """Guest credits on episodes 1011 and 1021 — two episodes of the same show,
    so the route has to scope by episode and not by show. Episode 1012 is left
    bare: 96% of the catalog has no guest cast at all.

    Rows are inserted out of credit order so the tests prove the route sorts by
    `credit_order` rather than falling back on insertion or id order. Guest stars
    are the one credit grain TMDB *does* send an `order` on, so this ordering
    survives the source change where show cast's did not.
    """
    session.add_all(
        [
            m.Person(id=70, tmdb_id=70, name="Guest Third"),
            m.Person(id=71, tmdb_id=71, name="Guest Lead"),
            m.Person(id=72, tmdb_id=72, name="Guest Second"),
            m.Character(id=80, show_id=1, name="Bartender"),
            m.Character(id=81, show_id=1, name="Herself"),
            m.Character(id=82, show_id=1, name="Neighbour"),
        ]
    )
    await session.flush()
    session.add_all(
        [
            m.EpisodeGuestCast(episode_id=1011, person_id=70, character_id=80, credit_order=2),
            m.EpisodeGuestCast(episode_id=1011, person_id=71, character_id=81, credit_order=0),
            m.EpisodeGuestCast(episode_id=1011, person_id=72, character_id=82, credit_order=1),
            m.EpisodeGuestCast(episode_id=1021, person_id=70, character_id=82, credit_order=0),
        ]
    )
    await session.commit()
    return client


async def test_guest_cast_returns_billing_order(seeded_guest_cast):
    r = await seeded_guest_cast.get("/episodes/1011/guest-cast")
    assert r.status_code == 200
    assert [c["person"]["name"] for c in r.json()] == [
        "Guest Lead",
        "Guest Second",
        "Guest Third",
    ]


async def test_guest_cast_entry_shape(seeded_guest_cast):
    r = await seeded_guest_cast.get("/episodes/1011/guest-cast")
    body = r.json()
    assert body[0] == {
        "person": {"id": 71, "name": "Guest Lead", "image_medium": None},
        "character": {"id": 81, "name": "Herself", "image_medium": None},
        "self": False,
        "voice": False,
    }
    assert body[2]["self"] is False and body[2]["voice"] is False
    assert body[2]["character"]["image_medium"] is None


async def test_guest_cast_is_scoped_to_one_episode(seeded_guest_cast):
    # Episode 1021 belongs to the same show as 1011 and has one guest credit of
    # its own, so a route scoped to the show rather than the episode would
    # return four entries here instead of one.
    r = await seeded_guest_cast.get("/episodes/1021/guest-cast")
    assert r.status_code == 200
    assert [c["person"]["name"] for c in r.json()] == ["Guest Third"]


async def test_episode_with_no_guest_cast_returns_empty_list_not_404(seeded_guest_cast):
    # 96% of episodes have zero guest cast. Empty is normal, not an error.
    r = await seeded_guest_cast.get("/episodes/1012/guest-cast")
    assert r.status_code == 200
    assert r.json() == []


async def test_unknown_episode_404s_for_guest_cast(client):
    r = await client.get("/episodes/999999/guest-cast")
    assert r.status_code == 404


async def test_guest_cast_cache_header_is_private(seeded_guest_cast):
    r = await seeded_guest_cast.get("/episodes/1011/guest-cast")
    assert r.headers["Cache-Control"] == "private, max-age=300"


async def test_guest_cast_requires_auth():
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/episodes/1011/guest-cast")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# /episodes/{id}/crew
# ---------------------------------------------------------------------------


@pytest.fixture
async def seeded_episode_crew(client, session):
    """Crew credits on episodes 1011 and 1021 — two episodes of the same show,
    so the route has to scope by episode and not by show. Episode 1012 is left
    bare: 22.5% of sampled episodes carry no crew credits at all.

    Rows are inserted out of job order so the tests prove the route sorts rather
    than falling back on insertion or id order. Person 91 holds two roles on
    episode 1011 — 36 of 1,043 sampled episodes do that.

    **Episode crew orders by job name**, because TMDB sends no `order` on a crew
    entry and `catalog.episode_crew` carries no `episode_count` either — there is
    no upstream signal left to sort on. Roles are also the same `crew_role` rows
    show crew uses: TMDB emits one `(department, job)` vocabulary at both grains.
    """
    session.add_all(
        [
            m.Person(id=90, tmdb_id=90, name="Crew Third"),
            m.Person(id=91, tmdb_id=91, name="Crew Lead"),
            m.CrewRole(id=95, department="Directing", job="Director"),
            m.CrewRole(id=96, department="Writing", job="Writer"),
            m.CrewRole(id=97, department="Writing", job="Story"),
        ]
    )
    await session.flush()
    session.add_all(
        [
            m.EpisodeCrew(episode_id=1011, person_id=90, role_id=97),
            m.EpisodeCrew(episode_id=1011, person_id=91, role_id=95),
            m.EpisodeCrew(episode_id=1011, person_id=91, role_id=96),
            m.EpisodeCrew(episode_id=1021, person_id=90, role_id=95),
        ]
    )
    await session.commit()
    return client


async def test_episode_crew_returns_job_order(seeded_episode_crew):
    r = await seeded_episode_crew.get("/episodes/1011/crew")
    assert r.status_code == 200
    assert r.json() == [
        {"person": {"id": 91, "name": "Crew Lead", "image_medium": None}, "role": "Director"},
        {"person": {"id": 90, "name": "Crew Third", "image_medium": None}, "role": "Story"},
        {"person": {"id": 91, "name": "Crew Lead", "image_medium": None}, "role": "Writer"},
    ]


async def test_one_person_in_two_roles_returns_two_entries(seeded_episode_crew):
    # Writer *and* director on the same episode is routine, and the three-part
    # unique key admits it — the route must not collapse the pair to one entry.
    body = (await seeded_episode_crew.get("/episodes/1011/crew")).json()
    assert [c["role"] for c in body if c["person"]["id"] == 91] == ["Director", "Writer"]


async def test_episode_crew_is_scoped_to_one_episode(seeded_episode_crew):
    # Episode 1021 belongs to the same show as 1011 and has one crew credit of
    # its own, so a route scoped to the show would return four entries here.
    r = await seeded_episode_crew.get("/episodes/1021/crew")
    assert r.status_code == 200
    assert [c["person"]["name"] for c in r.json()] == ["Crew Third"]


async def test_episode_with_no_crew_returns_empty_list_not_404(seeded_episode_crew):
    r = await seeded_episode_crew.get("/episodes/1012/crew")
    assert r.status_code == 200
    assert r.json() == []


async def test_unknown_episode_404s_for_crew(client):
    r = await client.get("/episodes/999999/crew")
    assert r.status_code == 404


async def test_episode_crew_cache_header_is_private(seeded_episode_crew):
    r = await seeded_episode_crew.get("/episodes/1011/crew")
    assert r.headers["Cache-Control"] == "private, max-age=300"


async def test_episode_crew_requires_auth():
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/episodes/1011/crew")
    assert r.status_code == 401
