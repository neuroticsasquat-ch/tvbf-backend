"""Integration tests for `GET /anticipated` (NEU-1059).

The read half of project spec §4, and a thin router over NEU-1058's query — so
what is asserted here is the route's own decisions rather than the ranking,
which `tests/integration/catalog/test_anticipated.py` already owns:

* **A show that has premiered cannot appear, structurally.** The date comparison
  lives in the query, so the list corrects itself as the clock passes a
  premiere; there is no job whose re-run it waits on.
* **Shows the viewer already tracks are marked, never filtered**, on
  `/trending`'s reasoning — a list of what is coming is a claim about the world.
* **Nothing matching is `200 []`**, not a 404 and not a 204.
* **Two queries**, whatever the length of the list: the list and the mark.
"""

from datetime import date, timedelta

import httpx
import pytest
from httpx import ASGITransport
from sqlalchemy import func, select

from tvbf.app.repos import show_membership_repo
from tvbf.catalog import models as m
from tvbf.main import app

# Ids well clear of the browse seed's 1..10.
_SOON = 400
_LATER = 401
_TRACKED = 402


@pytest.fixture
async def today(session) -> date:
    """Postgres's `current_date`, not Python's.

    The query compares against `current_date`, so seeding from `date.today()`
    would place every row below relative to a *different* clock than the one
    deciding the answer — and a timezone difference or a midnight rollover
    mid-test then flips the rows nearest the boundary. NEU-1058's tests state
    the same rule from the query's side.
    """
    return (await session.execute(select(func.current_date()))).scalar_one()


@pytest.fixture
async def seeded(session, today, authed_client):
    """Three future-dated shows, one of them tracked by the viewer."""
    session.add_all(
        [
            m.Show(
                id=_SOON,
                tmdb_id=_SOON,
                name="Soon And Awaited",
                first_air_date=today + timedelta(days=7),
                popularity=99.0,
            ),
            m.Show(
                id=_TRACKED,
                tmdb_id=_TRACKED,
                name="Already Tracked",
                first_air_date=today + timedelta(days=14),
                popularity=80.0,
            ),
            m.Show(
                id=_LATER,
                tmdb_id=_LATER,
                name="Later",
                first_air_date=today + timedelta(days=21),
                popularity=10.0,
            ),
        ]
    )
    await session.commit()
    return authed_client


async def test_serves_the_list_most_popular_first(seeded):
    r = await seeded.get("/anticipated")
    assert r.status_code == 200
    assert [s["id"] for s in r.json()] == [_SOON, _TRACKED, _LATER]


async def test_a_show_drops_off_when_the_clock_passes_its_premiere(seeded, session, today):
    """The acceptance criterion, and the reason this surface is a query.

    Moving the row back past `current_date` is the same experiment as moving
    the clock forward past the row — the two are only ever compared at read
    time — with the advantage of being runnable. Nothing else changes: no job
    runs, no snapshot is rewritten, and the very next request omits the show.
    """
    assert _SOON in {s["id"] for s in (await seeded.get("/anticipated")).json()}

    show = await session.get(m.Show, _SOON)
    assert show is not None
    show.first_air_date = today - timedelta(days=1)
    await session.commit()

    assert _SOON not in {s["id"] for s in (await seeded.get("/anticipated")).json()}


async def test_tracked_shows_are_marked_not_filtered(seeded, session, make_user):
    """The viewer's own membership marks its entry and removes nothing — and a
    stranger's membership on another entry marks nothing at all, which is what
    makes the flag per-viewer rather than per-show."""
    viewer = seeded.user  # type: ignore[attr-defined]
    stranger = await make_user(email="stranger@example.com")
    await show_membership_repo.add(session, user_id=viewer.id, show_id=_TRACKED)
    await show_membership_repo.add(session, user_id=stranger.id, show_id=_LATER)
    await session.commit()

    shows = (await seeded.get("/anticipated")).json()
    assert [s["id"] for s in shows] == [_SOON, _TRACKED, _LATER]
    assert {s["id"]: s["in_my_shows"] for s in shows} == {
        _SOON: False,
        _TRACKED: True,
        _LATER: False,
    }


async def test_nothing_matching_is_an_empty_list_not_a_404(authed_client):
    r = await authed_client.get("/anticipated")
    assert r.status_code == 200
    assert r.json() == []


async def test_entry_is_the_show_summary_shape(seeded):
    entry = (await seeded.get("/anticipated")).json()[0]
    # The keys `ShowCard` reads, so the SPA reuses it unchanged.
    for key in ("id", "name", "image_medium", "premiered", "rating_average", "my_rating"):
        assert key in entry
    assert entry["genres"] == []
    assert entry["network"] is None


async def test_cache_header_is_no_store(seeded):
    """`in_my_shows` is a per-user field, so this list is not one any cache may
    hold — the override `/trending` takes for the same reason."""
    r = await seeded.get("/anticipated")
    assert r.headers["Cache-Control"] == "private, no-store"


async def test_requires_auth():
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/anticipated")
    assert r.status_code == 401


async def test_serves_the_list_in_a_fixed_number_of_queries(seeded):
    """The list and the viewer's memberships: two, and neither moves with the
    length of the list. `my_rating` is what a third would buy, and this surface
    declines it (`AnticipatedShowOut`)."""
    from sqlalchemy import event

    from tvbf.db import engine as app_engine

    statements: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    engine = app_engine.sync_engine
    event.listen(engine, "before_cursor_execute", _record)
    try:
        r = await seeded.get("/anticipated")
    finally:
        event.remove(engine, "before_cursor_execute", _record)

    payload_queries = [s for s in statements if "catalog.show" in s or "user_show" in s]
    assert len(r.json()) == 3
    assert len(payload_queries) == 2, payload_queries
