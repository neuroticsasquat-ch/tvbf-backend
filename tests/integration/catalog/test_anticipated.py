"""Integration tests for the most-anticipated query (NEU-1058).

Project spec §4: a live SQL query over `catalog.show`, not a snapshot table and
not an upstream call. Four of the assertions below are the spec's decisions
rather than incidental behaviour:

* **The date comparison is in the query**, so a show cannot linger on the list
  after it premieres — there is no run whose output could go stale.
* **An undated show never appears**, however promising its status: 2,501 shows
  carry `Planned` / `In Production` / `Pilot` with no date, and there is no
  defensible position to sort them into.
* **`status` is deliberately not in the predicate**, so a future-dated
  `Returning Series` (*Lanterns*) is on the list.
* **There is no `vote_count` floor.** Of 408 future-dated shows in production,
  four have any votes at all — a floor there would empty the surface rather
  than clean it.
"""

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select

from tvbf.catalog import models as m
from tvbf.catalog.browse_queries import (
    ANTICIPATED_LIMIT,
    ANTICIPATED_WINDOW_DAYS,
    list_anticipated_shows,
)


@pytest.fixture
async def today(session) -> date:
    """Postgres's `current_date`, not Python's.

    The query compares against `current_date`, so seeding from `date.today()`
    would make these assertions depend on the skew between two clocks — and
    every row below is placed relative to the boundary, so a timezone
    difference or a midnight rollover mid-test flips them. Same rule
    `get_trending_snapshot` states for its cutoff, applied from the other side.
    """
    return (await session.execute(select(func.current_date()))).scalar_one()


def _show(show_id: int, name: str, **kwargs) -> m.Show:
    return m.Show(id=show_id, tmdb_id=show_id, name=name, **kwargs)


@pytest.fixture
async def seeded(session, today):
    """One eligible show per rank, plus one of every reason to be excluded."""
    session.add_all(
        [
            _show(1, "Mid", first_air_date=today + timedelta(days=30), popularity=50.0),
            _show(2, "Most Awaited", first_air_date=today + timedelta(days=10), popularity=99.0),
            _show(3, "Unscored", first_air_date=today + timedelta(days=20), popularity=None),
            _show(4, "Premiering Today", first_air_date=today, popularity=70.0),
            _show(5, "Already Out", first_air_date=today - timedelta(days=1), popularity=500.0),
            _show(
                6, "Undated Pilot", first_air_date=None, status="In Production", popularity=400.0
            ),
            _show(
                7,
                "Far Future",
                first_air_date=today + timedelta(days=ANTICIPATED_WINDOW_DAYS),
                popularity=300.0,
            ),
            _show(
                8,
                "Gone Upstream",
                first_air_date=today + timedelta(days=5),
                popularity=200.0,
                deleted_upstream_at=datetime.now(tz=UTC),
            ),
            _show(
                9,
                "Adult",
                first_air_date=today + timedelta(days=5),
                popularity=200.0,
                adult=True,
            ),
        ]
    )
    await session.flush()


async def test_ranks_by_popularity_with_unscored_shows_last(session, seeded):
    shows = await list_anticipated_shows(session)
    assert [s.name for s in shows] == ["Most Awaited", "Premiering Today", "Mid", "Unscored"]


async def test_a_show_that_has_already_premiered_never_appears(session, seeded):
    shows = await list_anticipated_shows(session)
    # Seeded with the highest popularity of the lot, so only the date excludes it.
    assert "Already Out" not in {s.name for s in shows}


async def test_an_undated_show_never_appears(session, seeded):
    shows = await list_anticipated_shows(session)
    assert "Undated Pilot" not in {s.name for s in shows}


async def test_status_is_not_in_the_predicate(session, today):
    """*Lanterns*: `Returning Series` with a future first air date belongs here."""
    session.add(
        _show(
            20,
            "Lanterns",
            first_air_date=today + timedelta(days=40),
            status="Returning Series",
            popularity=10.0,
        )
    )
    await session.flush()
    assert [s.name for s in await list_anticipated_shows(session)] == ["Lanterns"]


async def test_tombstoned_and_adult_shows_never_appear(session, seeded):
    names = {s.name for s in await list_anticipated_shows(session)}
    assert "Gone Upstream" not in names
    assert "Adult" not in names


async def test_the_window_excludes_a_show_dated_past_it(session, seeded):
    assert "Far Future" not in {s.name for s in await list_anticipated_shows(session)}


async def test_the_window_is_configurable(session, seeded):
    shows = await list_anticipated_shows(session, window_days=15)
    assert [s.name for s in shows] == ["Most Awaited", "Premiering Today"]


async def test_the_length_is_configurable(session, seeded):
    shows = await list_anticipated_shows(session, limit=2)
    assert [s.name for s in shows] == ["Most Awaited", "Premiering Today"]


async def test_the_default_length_caps_the_list(session, today):
    for offset in range(ANTICIPATED_LIMIT + 5):
        session.add(
            _show(
                100 + offset,
                f"Upcoming {offset:02d}",
                first_air_date=today + timedelta(days=1 + offset),
                popularity=float(offset),
            )
        )
    await session.flush()
    assert len(await list_anticipated_shows(session)) == ANTICIPATED_LIMIT


async def test_ties_break_deterministically(session, today):
    """`ORDER BY popularity` alone is a partial order, so two shows carrying the
    same score may come back either way round; the id is what settles it."""
    for show_id in (33, 31, 32):
        session.add(
            _show(
                show_id,
                f"Tied {show_id}",
                first_air_date=today + timedelta(days=7),
                popularity=0.0,
            )
        )
    await session.flush()
    assert [s.id for s in await list_anticipated_shows(session)] == [31, 32, 33]
