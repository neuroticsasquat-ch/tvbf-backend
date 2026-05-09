"""Tests for `my_shows_service.list_watched` (NEU-102)."""

from datetime import UTC, date, datetime, timedelta

import pytest

from tvbf.app.models import UserEpisodeWatch, UserShowWatch
from tvbf.app.services import my_shows_service
from tvbf.tvmaze.models import Episode, Show


async def _seed_show(
    session,
    *,
    show_id: int,
    name: str = "S",
    episodes: int = 3,
    show_status: str = "Ended",
    airdates: list[date] | None = None,
) -> Show:
    show = Show(id=show_id, name=name, tvmaze_updated=1, status=show_status)
    session.add(show)
    await session.flush()
    today = date.today()
    for i in range(1, episodes + 1):
        ad = (
            airdates[i - 1]
            if airdates is not None and i - 1 < len(airdates)
            else today - timedelta(days=episodes - i + 1)
        )
        session.add(Episode(id=show_id * 100 + i, show_id=show.id, season=1, number=i, airdate=ad))
    await session.flush()
    return show


async def _watch(session, user_id, episode_id):
    session.add(
        UserEpisodeWatch(user_id=user_id, episode_id=episode_id, watched_at=datetime.now(UTC))
    )


@pytest.mark.asyncio
async def test_returns_only_shows_with_at_least_one_watch(session, make_user):
    user = await make_user()
    a = await _seed_show(session, show_id=940001, name="Watched", episodes=2)
    b = await _seed_show(session, show_id=940002, name="Untouched", episodes=2)
    await _watch(session, user.id, a.id * 100 + 1)
    await session.commit()

    rows = await my_shows_service.list_watched(session, user_id=user.id)
    ids = [e.show.id for e in rows]
    assert ids == [a.id]
    assert b.id not in ids


@pytest.mark.asyncio
async def test_ended_show_fully_watched_is_finished(session, make_user):
    user = await make_user()
    show = await _seed_show(session, show_id=940010, name="Done", episodes=2, show_status="Ended")
    await _watch(session, user.id, show.id * 100 + 1)
    await _watch(session, user.id, show.id * 100 + 2)
    await session.commit()

    rows = await my_shows_service.list_watched(session, user_id=user.id)
    assert len(rows) == 1
    assert rows[0].status == "finished"


@pytest.mark.asyncio
async def test_running_show_fully_watched_is_in_progress(session, make_user):
    """A still-airing show fully caught up stays in 'in_progress' until it ends."""
    user = await make_user()
    show = await _seed_show(
        session, show_id=940011, name="Active", episodes=2, show_status="Running"
    )
    await _watch(session, user.id, show.id * 100 + 1)
    await _watch(session, user.id, show.id * 100 + 2)
    await session.commit()

    rows = await my_shows_service.list_watched(session, user_id=user.id)
    assert len(rows) == 1
    assert rows[0].status == "in_progress"


@pytest.mark.asyncio
async def test_partial_watch_is_in_progress(session, make_user):
    user = await make_user()
    show = await _seed_show(
        session, show_id=940012, name="Partial", episodes=3, show_status="Ended"
    )
    await _watch(session, user.id, show.id * 100 + 1)
    await session.commit()

    rows = await my_shows_service.list_watched(session, user_id=user.id)
    assert len(rows) == 1
    assert rows[0].status == "in_progress"


@pytest.mark.asyncio
async def test_status_filter_finished(session, make_user):
    user = await make_user()
    finished = await _seed_show(
        session, show_id=940020, name="Finished", episodes=1, show_status="Ended"
    )
    in_progress = await _seed_show(
        session, show_id=940021, name="WIP", episodes=2, show_status="Ended"
    )
    await _watch(session, user.id, finished.id * 100 + 1)
    await _watch(session, user.id, in_progress.id * 100 + 1)
    await session.commit()

    rows = await my_shows_service.list_watched(session, user_id=user.id, status="finished")
    assert [e.show.id for e in rows] == [finished.id]


@pytest.mark.asyncio
async def test_status_filter_in_progress(session, make_user):
    user = await make_user()
    finished = await _seed_show(
        session, show_id=940030, name="Finished", episodes=1, show_status="Ended"
    )
    in_progress = await _seed_show(
        session, show_id=940031, name="WIP", episodes=2, show_status="Ended"
    )
    await _watch(session, user.id, finished.id * 100 + 1)
    await _watch(session, user.id, in_progress.id * 100 + 1)
    await session.commit()

    rows = await my_shows_service.list_watched(session, user_id=user.id, status="in_progress")
    assert [e.show.id for e in rows] == [in_progress.id]


@pytest.mark.asyncio
async def test_in_my_shows_flag(session, make_user):
    user = await make_user()
    tracked = await _seed_show(
        session, show_id=940040, name="Tracked", episodes=2, show_status="Ended"
    )
    untracked = await _seed_show(
        session, show_id=940041, name="Untracked", episodes=2, show_status="Ended"
    )
    session.add(UserShowWatch(user_id=user.id, show_id=tracked.id))
    await _watch(session, user.id, tracked.id * 100 + 1)
    await _watch(session, user.id, untracked.id * 100 + 1)
    await session.commit()

    rows = await my_shows_service.list_watched(session, user_id=user.id)
    by_id = {e.show.id: e for e in rows}
    assert by_id[tracked.id].in_my_shows is True
    assert by_id[untracked.id].in_my_shows is False


@pytest.mark.asyncio
async def test_user_scoped_results(session, make_user):
    a = await make_user(email="wa@example.com", display_name="A")
    b = await make_user(email="wb@example.com", display_name="B")
    show_a = await _seed_show(session, show_id=940050, name="A only", episodes=1)
    show_b = await _seed_show(session, show_id=940051, name="B only", episodes=1)
    await _watch(session, a.id, show_a.id * 100 + 1)
    await _watch(session, b.id, show_b.id * 100 + 1)
    await session.commit()

    rows_a = await my_shows_service.list_watched(session, user_id=a.id)
    rows_b = await my_shows_service.list_watched(session, user_id=b.id)
    assert [e.show.id for e in rows_a] == [show_a.id]
    assert [e.show.id for e in rows_b] == [show_b.id]


@pytest.mark.asyncio
async def test_empty_when_user_has_no_watches(session, make_user):
    user = await make_user()
    await _seed_show(session, show_id=940060, name="Catalog", episodes=1)
    await session.commit()

    rows = await my_shows_service.list_watched(session, user_id=user.id)
    assert rows == []


@pytest.mark.asyncio
async def test_default_sort_last_watched_desc(session, make_user):
    user = await make_user()
    older = await _seed_show(session, show_id=940070, name="Older", episodes=1, show_status="Ended")
    newer = await _seed_show(session, show_id=940071, name="Newer", episodes=1, show_status="Ended")
    now = datetime.now(UTC)
    session.add(
        UserEpisodeWatch(
            user_id=user.id,
            episode_id=older.id * 100 + 1,
            watched_at=now - timedelta(days=10),
        )
    )
    session.add(UserEpisodeWatch(user_id=user.id, episode_id=newer.id * 100 + 1, watched_at=now))
    await session.commit()

    rows = await my_shows_service.list_watched(session, user_id=user.id)
    assert [e.show.id for e in rows] == [newer.id, older.id]


@pytest.mark.asyncio
async def test_sort_name_asc(session, make_user):
    user = await make_user()
    z = await _seed_show(session, show_id=940080, name="Zoot", episodes=1, show_status="Ended")
    a = await _seed_show(session, show_id=940081, name="Alpha", episodes=1, show_status="Ended")
    await _watch(session, user.id, z.id * 100 + 1)
    await _watch(session, user.id, a.id * 100 + 1)
    await session.commit()

    rows = await my_shows_service.list_watched(session, user_id=user.id, sort="name_asc")
    assert [e.show.id for e in rows] == [a.id, z.id]
