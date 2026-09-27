"""`recompute_last_aired` — the one definition of **Last aired** on the spine (NEU-1502)."""

from datetime import date, timedelta

from sqlalchemy import select, update

from tvbf.catalog import models as m
from tvbf.catalog.last_aired import recompute_last_aired

TODAY = date(2026, 9, 27)
AIRED = TODAY - timedelta(days=30)

SHOW_ID = 963_000


async def _show(session, show_id: int, *episodes: tuple[int, int, date | None]) -> None:
    """A show and its episodes as `(season_number, episode_number, air_date)`."""
    session.add(m.Show(id=show_id, name=f"Show {show_id}"))
    await session.flush()
    session.add_all(
        m.Episode(
            id=show_id * 100 + i,
            show_id=show_id,
            season_number=season,
            episode_number=number,
            air_date=air_date,
        )
        for i, (season, number, air_date) in enumerate(episodes)
    )
    await session.flush()


async def _last_aired(session, show_id: int) -> date | None:
    return (
        await session.execute(
            select(m.Show.last_aired)
            .where(m.Show.id == show_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def test_takes_the_latest_regular_episode_on_or_before_today(session):
    await _show(session, SHOW_ID, (1, 1, AIRED), (1, 2, TODAY), (1, 3, None))

    assert await recompute_last_aired(session, today=TODAY) >= 1
    assert await _last_aired(session, SHOW_ID) == TODAY


async def test_ignores_a_later_special(session):
    """Both kinds — TMDB's season 0 and a copied negative number (NEU-1062)."""
    later = TODAY - timedelta(days=1)
    await _show(session, SHOW_ID, (1, 1, AIRED), (0, 1, later), (1, -1, later))

    await recompute_last_aired(session, today=TODAY)
    assert await _last_aired(session, SHOW_ID) == AIRED


async def test_ignores_a_future_dated_regular_episode(session):
    await _show(session, SHOW_ID, (1, 1, AIRED), (1, 2, TODAY + timedelta(days=1)))

    await recompute_last_aired(session, today=TODAY)
    assert await _last_aired(session, SHOW_ID) == AIRED


async def test_nulls_a_show_with_no_qualifying_episode(session):
    """A stored value with nothing behind it any more is cleared, not left stale."""
    await _show(session, SHOW_ID, (0, 1, AIRED), (1, 1, TODAY + timedelta(days=7)))
    session.add(m.Show(id=SHOW_ID + 1, name="No episodes", last_aired=AIRED))
    await session.flush()
    await session.execute(update(m.Show).where(m.Show.id == SHOW_ID).values(last_aired=AIRED))

    await recompute_last_aired(session, today=TODAY)
    assert await _last_aired(session, SHOW_ID) is None
    assert await _last_aired(session, SHOW_ID + 1) is None


async def test_rolls_forward_as_the_day_changes(session):
    """No row changes between the two calls — only `today` does."""
    tomorrow = TODAY + timedelta(days=1)
    await _show(session, SHOW_ID, (1, 1, AIRED), (1, 2, tomorrow))

    await recompute_last_aired(session, today=TODAY)
    assert await _last_aired(session, SHOW_ID) == AIRED
    await recompute_last_aired(session, today=tomorrow)
    assert await _last_aired(session, SHOW_ID) == tomorrow


async def test_scopes_to_show_ids_when_given(session):
    await _show(session, SHOW_ID, (1, 1, AIRED))
    await _show(session, SHOW_ID + 1, (1, 1, AIRED))

    assert await recompute_last_aired(session, today=TODAY, show_ids=[SHOW_ID]) == 1
    assert await _last_aired(session, SHOW_ID) == AIRED
    assert await _last_aired(session, SHOW_ID + 1) is None


async def test_writes_only_the_rows_that_moved(session):
    await _show(session, SHOW_ID, (1, 1, AIRED))

    assert await recompute_last_aired(session, today=TODAY, show_ids=[SHOW_ID]) == 1
    assert await recompute_last_aired(session, today=TODAY, show_ids=[SHOW_ID]) == 0
