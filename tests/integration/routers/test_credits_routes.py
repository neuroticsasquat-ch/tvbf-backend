"""Integration tests for the credit browse routes: show cast/crew (NEU-940),
episode guest cast (NEU-949), episode crew (NEU-963), and the regular/guest and
series/episode splits plus the season routes built on them (NEU-1512).

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

Since NEU-1512 **a regular is a (person, character) with a `season_cast` row**
on one of the show's seasons, and every other `show_cast` row is a guest; a
**series crew credit** is a `show_crew` job whose `episode_count` exceeds the
person's `episode_crew` rows in that role on that show (spec §2.1, §2.2).
"""

import httpx
import pytest
from httpx import ASGITransport

from tests.fixtures.browse.seed import seed
from tvbf.catalog import models as m
from tvbf.main import app

LEAD, SECOND, THIRD, NEWCOMER, CAMEO, DIRECTOR, WRITER, COURIER = 11, 12, 10, 13, 14, 15, 16, 17


@pytest.fixture
async def client(authed_client, session):
    """Authed ASGI client with the browse seed loaded."""
    await seed(session)
    yield authed_client


@pytest.fixture
async def seeded_credits(client, session):
    """Credits for show 1 (seasons 101/102, episodes 1011, 1012, 1021, 1022).
    Show 2 is left bare — 27% of the catalog has none.

    Who is what, and why each is here:

    * **Lead** (Hero) — regular in seasons 1 and 2, so on `/cast` exactly once;
      also credited as a guest on 1011, which season 1's own regular list
      overrides at season grain.
    * **Second** (Villain) — regular in season 1, guest in season 2 as the same
      character. One `show_cast` row covers both, so a regular at show grain and
      a guest at season 2's.
    * **Newcomer** (Rookie) — regular in season 2 with no `show_cast` row: the
      parity gap, served with `episode_count: null`, last.
    * **Third** (Sidekick), **Cameo** (Waiter), **Courier** — guests only.

    Crew is the §2.2 split's three cases: Writer has more aggregate episodes than
    episode rows (series crew), Creator and Executive Producer have no episode
    rows at all (series crew), Director's count equals its rows (episode crew).

    Rows are inserted out of order throughout, so the tests prove the routes
    sort rather than echo insertion or id order.
    """
    session.add_all(
        [
            m.Person(id=THIRD, tmdb_id=THIRD, name="Third"),
            m.Person(id=LEAD, tmdb_id=LEAD, name="Lead"),
            m.Person(id=SECOND, tmdb_id=SECOND, name="Second"),
            m.Person(id=NEWCOMER, tmdb_id=NEWCOMER, name="Newcomer"),
            m.Person(id=CAMEO, tmdb_id=CAMEO, name="Cameo"),
            m.Person(id=DIRECTOR, tmdb_id=DIRECTOR, name="Director Person"),
            m.Person(id=WRITER, tmdb_id=WRITER, name="Writer Person"),
            m.Person(id=COURIER, tmdb_id=COURIER, name="Courier"),
            m.Character(id=20, show_id=1, name="Sidekick"),
            m.Character(id=21, show_id=1, name="Hero"),
            m.Character(id=22, show_id=1, name="Villain"),
            m.Character(id=23, show_id=1, name="Rookie"),
            m.Character(id=24, show_id=1, name="Waiter"),
            m.Character(id=25, show_id=1, name="Courier"),
            m.CrewRole(id=30, department="Production", job="Executive Producer"),
            m.CrewRole(id=31, department="Writing", job="Creator"),
            m.CrewRole(id=32, department="Directing", job="Director"),
            m.CrewRole(id=33, department="Writing", job="Writer"),
        ]
    )
    await session.flush()
    session.add_all(
        [
            m.ShowCast(
                show_id=1, person_id=THIRD, character_id=20, episode_count=2, billing_order=5
            ),
            m.ShowCast(show_id=1, person_id=CAMEO, character_id=24, episode_count=1),
            m.ShowCast(
                show_id=1, person_id=LEAD, character_id=21, episode_count=3, billing_order=0
            ),
            m.ShowCast(
                show_id=1, person_id=COURIER, character_id=25, episode_count=1, billing_order=3
            ),
            m.ShowCast(
                show_id=1, person_id=SECOND, character_id=22, episode_count=2, billing_order=1
            ),
            m.SeasonCast(season_id=102, person_id=NEWCOMER, character_id=23, billing_order=1),
            m.SeasonCast(season_id=101, person_id=SECOND, character_id=22, billing_order=1),
            m.SeasonCast(season_id=101, person_id=LEAD, character_id=21, billing_order=0),
            m.SeasonCast(season_id=102, person_id=LEAD, character_id=21, billing_order=0),
            m.EpisodeGuestCast(episode_id=1011, person_id=THIRD, character_id=20, credit_order=1),
            m.EpisodeGuestCast(episode_id=1012, person_id=CAMEO, character_id=24, credit_order=1),
            m.EpisodeGuestCast(episode_id=1012, person_id=THIRD, character_id=20, credit_order=0),
            m.EpisodeGuestCast(episode_id=1011, person_id=LEAD, character_id=21, credit_order=0),
            m.EpisodeGuestCast(episode_id=1021, person_id=COURIER, character_id=25, credit_order=1),
            m.EpisodeGuestCast(episode_id=1021, person_id=SECOND, character_id=22, credit_order=0),
            m.ShowCrew(show_id=1, person_id=THIRD, role_id=30, episode_count=1),
            m.ShowCrew(show_id=1, person_id=DIRECTOR, role_id=32, episode_count=2),
            m.ShowCrew(show_id=1, person_id=LEAD, role_id=31, episode_count=2),
            m.ShowCrew(show_id=1, person_id=WRITER, role_id=33, episode_count=3),
            m.EpisodeCrew(episode_id=1011, person_id=WRITER, role_id=33),
            m.EpisodeCrew(episode_id=1012, person_id=DIRECTOR, role_id=32),
            m.EpisodeCrew(episode_id=1011, person_id=DIRECTOR, role_id=32),
        ]
    )
    await session.commit()
    return client


def _people(body: list[dict]) -> list[str]:
    return [c["person"]["name"] for c in body]


def _pairs(body: list[dict]) -> list[tuple[int, int]]:
    return [(c["person"]["id"], c["character"]["id"]) for c in body]


# ---------------------------------------------------------------------------
# /shows/{id}/cast — regular credits
# ---------------------------------------------------------------------------


async def test_cast_returns_regulars_by_episode_count(seeded_credits):
    # Lead regulars in two seasons and is listed once; Newcomer has no aggregate
    # row, so no count, so last.
    r = await seeded_credits.get("/shows/1/cast")
    assert r.status_code == 200
    assert _people(r.json()) == ["Lead", "Second", "Newcomer"]


async def test_cast_entry_shape(seeded_credits):
    r = await seeded_credits.get("/shows/1/cast")
    body = r.json()
    assert body[0] == {
        "person": {"id": LEAD, "name": "Lead", "image_medium": None},
        "character": {"id": 21, "name": "Hero", "image_medium": None},
        "self": False,
        "voice": False,
        "episode_count": 3,
    }
    # `self`, `voice` and a character image have no TMDB counterpart at all, so
    # they are false/null on every entry rather than only on this one.
    assert body[1]["self"] is False and body[1]["voice"] is False
    assert body[1]["character"]["image_medium"] is None


async def test_cast_breaks_an_episode_count_tie_on_billing_order(seeded_credits, session):
    # A second regular level with Lead on three episodes, billed after them.
    session.add_all(
        [
            m.Person(id=18, tmdb_id=18, name="Co-Lead"),
            m.Character(id=26, show_id=1, name="Partner"),
        ]
    )
    await session.flush()
    session.add_all(
        [
            m.ShowCast(show_id=1, person_id=18, character_id=26, episode_count=3, billing_order=2),
            m.SeasonCast(season_id=101, person_id=18, character_id=26, billing_order=2),
        ]
    )
    await session.commit()
    body = (await seeded_credits.get("/shows/1/cast")).json()
    assert _people(body) == ["Lead", "Co-Lead", "Second", "Newcomer"]


async def test_regular_with_no_aggregate_row_has_null_count(seeded_credits):
    body = (await seeded_credits.get("/shows/1/cast")).json()
    assert body[-1]["person"]["id"] == NEWCOMER
    assert body[-1]["episode_count"] is None


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
# /shows/{id}/guest-cast — every other show_cast row
# ---------------------------------------------------------------------------


async def test_guest_cast_returns_the_rest_by_episode_count(seeded_credits):
    # Courier and Cameo tie on count; billing order breaks it, nulls last.
    r = await seeded_credits.get("/shows/1/guest-cast")
    assert r.status_code == 200
    assert _people(r.json()) == ["Third", "Courier", "Cameo"]
    assert [c["episode_count"] for c in r.json()] == [2, 1, 1]


async def test_regular_guesting_in_another_season_stays_off_guest_cast(seeded_credits):
    # Second guests in season 2 as the Villain they regular as in season 1:
    # one aggregate row, and it is a regular's.
    body = (await seeded_credits.get("/shows/1/guest-cast")).json()
    assert SECOND not in [c["person"]["id"] for c in body]


async def test_cast_and_guest_cast_partition_show_cast(seeded_credits, session):
    cast = _pairs((await seeded_credits.get("/shows/1/cast")).json())
    guests = _pairs((await seeded_credits.get("/shows/1/guest-cast")).json())
    show_cast = {
        (row.person_id, row.character_id)
        for row in (await session.execute(m.ShowCast.__table__.select())).all()
    }
    # Every show_cast row exactly once; the regular with no aggregate row is the
    # one addition.
    assert len(cast) == len(set(cast)) and len(guests) == len(set(guests))
    assert not set(cast) & set(guests)
    assert (set(cast) | set(guests)) - {(NEWCOMER, 23)} == show_cast


async def test_show_with_no_guest_cast_returns_empty_list(seeded_credits):
    r = await seeded_credits.get("/shows/2/guest-cast")
    assert r.status_code == 200
    assert r.json() == []


async def test_unknown_show_404s_for_guest_cast(client):
    assert (await client.get("/shows/999999/guest-cast")).status_code == 404


async def test_guest_cast_cache_header_and_auth(seeded_credits):
    r = await seeded_credits.get("/shows/1/guest-cast")
    assert r.headers["Cache-Control"] == "private, max-age=300"
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        assert (await c.get("/shows/1/guest-cast")).status_code == 401


# ---------------------------------------------------------------------------
# /shows/{id}/crew — series crew; /shows/{id}/episode-crew — the rest
# ---------------------------------------------------------------------------


async def test_crew_returns_series_crew_by_episode_count(seeded_credits):
    r = await seeded_credits.get("/shows/1/crew")
    assert r.status_code == 200
    assert r.json() == [
        {
            "person": {"id": WRITER, "name": "Writer Person", "image_medium": None},
            "role": "Writer",
            "episode_count": 3,
        },
        {
            "person": {"id": LEAD, "name": "Lead", "image_medium": None},
            "role": "Creator",
            "episode_count": 2,
        },
        {
            "person": {"id": THIRD, "name": "Third", "image_medium": None},
            "role": "Executive Producer",
            "episode_count": 1,
        },
    ]


async def test_episode_crew_returns_jobs_fully_covered_by_episode_rows(seeded_credits):
    # Director's two aggregate episodes are its two episode rows: a sum, not a
    # series credit.
    r = await seeded_credits.get("/shows/1/episode-crew")
    assert r.status_code == 200
    assert r.json() == [
        {
            "person": {"id": DIRECTOR, "name": "Director Person", "image_medium": None},
            "role": "Director",
            "episode_count": 2,
        }
    ]


async def test_crew_and_episode_crew_partition_show_crew(seeded_credits, session):
    def keys(body):
        return [(c["person"]["id"], c["role"]) for c in body]

    crew = keys((await seeded_credits.get("/shows/1/crew")).json())
    episode_crew = keys((await seeded_credits.get("/shows/1/episode-crew")).json())
    jobs = {30: "Executive Producer", 31: "Creator", 32: "Director", 33: "Writer"}
    show_crew = {
        (row.person_id, jobs[row.role_id])
        for row in (await session.execute(m.ShowCrew.__table__.select())).all()
    }
    assert len(crew) + len(episode_crew) == len(show_crew)
    assert set(crew) | set(episode_crew) == show_crew


async def test_show_with_no_crew_returns_empty_list_not_404(seeded_credits):
    for path in ("/shows/2/crew", "/shows/2/episode-crew"):
        r = await seeded_credits.get(path)
        assert r.status_code == 200
        assert r.json() == []


async def test_unknown_show_404s_for_crew(client):
    assert (await client.get("/shows/999999/crew")).status_code == 404
    assert (await client.get("/shows/999999/episode-crew")).status_code == 404


async def test_crew_cache_header_is_private(seeded_credits):
    for path in ("/shows/1/crew", "/shows/1/episode-crew"):
        r = await seeded_credits.get(path)
        assert r.headers["Cache-Control"] == "private, max-age=300"


async def test_crew_requires_auth():
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        assert (await c.get("/shows/1/crew")).status_code == 401
        assert (await c.get("/shows/1/episode-crew")).status_code == 401


# ---------------------------------------------------------------------------
# /shows/{id}/seasons/{number}/cast and /crew
# ---------------------------------------------------------------------------


async def test_season_cast_splits_regulars_from_guests(seeded_credits):
    r = await seeded_credits.get("/shows/1/seasons/1/cast")
    assert r.status_code == 200
    body = r.json()
    # Regulars in billing order, no count — TMDB's "every episode" is a claim.
    assert _people(body["regulars"]) == ["Lead", "Second"]
    assert [c["episode_count"] for c in body["regulars"]] == [None, None]
    # Guests by appearances in this season. Lead's guest row on 1011 is dropped:
    # the season's own regular list wins.
    assert _people(body["guests"]) == ["Third", "Cameo"]
    assert [c["episode_count"] for c in body["guests"]] == [2, 1]


async def test_season_guests_are_scoped_to_the_season(seeded_credits):
    # Courier guests only in season 2; Second regulars in season 1 and guests in
    # season 2, so is a guest here.
    body = (await seeded_credits.get("/shows/1/seasons/2/cast")).json()
    assert _people(body["regulars"]) == ["Lead", "Newcomer"]
    assert _people(body["guests"]) == ["Second", "Courier"]
    assert "Courier" not in _people(
        (await seeded_credits.get("/shows/1/seasons/1/cast")).json()["guests"]
    )


async def test_season_crew_groups_by_person_and_role(seeded_credits):
    r = await seeded_credits.get("/shows/1/seasons/1/crew")
    assert r.status_code == 200
    assert r.json() == [
        {
            "person": {"id": DIRECTOR, "name": "Director Person", "image_medium": None},
            "role": "Director",
            "episode_count": 2,
        },
        {
            "person": {"id": WRITER, "name": "Writer Person", "image_medium": None},
            "role": "Writer",
            "episode_count": 1,
        },
    ]
    assert (await seeded_credits.get("/shows/1/seasons/2/crew")).json() == []


async def test_specials_season_is_served_like_any_other(seeded_credits, session):
    session.add(m.Season(id=100, tmdb_id=100, show_id=1, season_number=0))
    await session.flush()
    session.add(
        m.Episode(
            id=1001, tmdb_id=1001, show_id=1, season_id=100, season_number=0, episode_number=1
        )
    )
    await session.flush()
    session.add_all(
        [
            m.SeasonCast(season_id=100, person_id=THIRD, character_id=20, billing_order=0),
            m.EpisodeGuestCast(episode_id=1001, person_id=CAMEO, character_id=24),
            m.EpisodeCrew(episode_id=1001, person_id=WRITER, role_id=33),
        ]
    )
    await session.commit()

    body = (await seeded_credits.get("/shows/1/seasons/0/cast")).json()
    assert _people(body["regulars"]) == ["Third"]
    assert _people(body["guests"]) == ["Cameo"]
    assert _people((await seeded_credits.get("/shows/1/seasons/0/crew")).json()) == [
        "Writer Person"
    ]


async def test_duplicated_season_number_resolves_to_the_seasons_route_row(seeded_credits, session):
    # A copied TV Maze row at number 2, with a lower id than the ingested 102.
    # `catalog/seasons.py:deduped` prefers the ingested row; so must this route.
    session.add(m.Season(id=99, tmdb_id=None, show_id=1, season_number=2))
    await session.flush()
    session.add(m.SeasonCast(season_id=99, person_id=THIRD, character_id=20, billing_order=0))
    await session.commit()

    picked = [
        s["id"] for s in (await seeded_credits.get("/shows/1/seasons")).json() if s["number"] == 2
    ]
    assert picked == [102]
    body = (await seeded_credits.get("/shows/1/seasons/2/cast")).json()
    assert _people(body["regulars"]) == ["Lead", "Newcomer"]


async def test_season_with_no_credits_returns_empty_lists(seeded_credits):
    r = await seeded_credits.get("/shows/2/seasons/1/cast")
    assert r.status_code == 200
    assert r.json() == {"regulars": [], "guests": []}


async def test_unknown_season_or_show_404s(client):
    for path in (
        "/shows/1/seasons/9/cast",
        "/shows/1/seasons/9/crew",
        "/shows/999999/seasons/1/cast",
        "/shows/999999/seasons/1/crew",
    ):
        assert (await client.get(path)).status_code == 404, path


async def test_season_routes_cache_header_and_auth(seeded_credits):
    for path in ("/shows/1/seasons/1/cast", "/shows/1/seasons/1/crew"):
        r = await seeded_credits.get(path)
        assert r.headers["Cache-Control"] == "private, max-age=300"
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        assert (await c.get("/shows/1/seasons/1/cast")).status_code == 401
        assert (await c.get("/shows/1/seasons/1/crew")).status_code == 401


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
        # One appearance by definition, so no count (NEU-1512 §4.1).
        "episode_count": None,
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
    lead = {"id": 91, "name": "Crew Lead", "image_medium": None}
    third = {"id": 90, "name": "Crew Third", "image_medium": None}
    # `episode_count` is null at episode grain: one credit is one episode.
    assert r.json() == [
        {"person": lead, "role": "Director", "episode_count": None},
        {"person": third, "role": "Story", "episode_count": None},
        {"person": lead, "role": "Writer", "episode_count": None},
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
