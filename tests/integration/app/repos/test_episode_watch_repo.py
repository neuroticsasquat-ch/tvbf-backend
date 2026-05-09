"""Repo-level tests for `episode_watch_repo.watched_in` (NEU-100 Path B helper)."""

from datetime import UTC, datetime

import pytest

from tvbf.app.models import UserEpisodeWatch
from tvbf.app.repos import episode_watch_repo
from tvbf.tvmaze.models import Episode, Show


async def _seed(session, *, show_id: int, episodes: int = 3) -> Show:
    show = Show(id=show_id, name="S", tvmaze_updated=1)
    session.add(show)
    await session.flush()
    for i in range(1, episodes + 1):
        session.add(Episode(id=show_id * 100 + i, show_id=show.id, season=1, number=i))
    await session.flush()
    return show


@pytest.mark.asyncio
async def test_watched_in_returns_subset(session, make_user):
    user = await make_user()
    show = await _seed(session, show_id=920001, episodes=3)
    # Mark episodes 1 and 3 as watched, leave 2 unwatched.
    session.add(
        UserEpisodeWatch(
            user_id=user.id, episode_id=show.id * 100 + 1, watched_at=datetime.now(UTC)
        )
    )
    session.add(
        UserEpisodeWatch(
            user_id=user.id, episode_id=show.id * 100 + 3, watched_at=datetime.now(UTC)
        )
    )
    await session.commit()

    candidates = [show.id * 100 + 1, show.id * 100 + 2, show.id * 100 + 3]
    watched = await episode_watch_repo.watched_in(session, user_id=user.id, episode_ids=candidates)
    assert watched == {show.id * 100 + 1, show.id * 100 + 3}


@pytest.mark.asyncio
async def test_watched_in_empty_input(session, make_user):
    user = await make_user()
    await session.commit()
    assert await episode_watch_repo.watched_in(session, user_id=user.id, episode_ids=[]) == set()


@pytest.mark.asyncio
async def test_watched_in_user_scoped(session, make_user):
    user_a = await make_user(email="ua@example.com", display_name="UA")
    user_b = await make_user(email="ub@example.com", display_name="UB")
    show = await _seed(session, show_id=920002, episodes=2)
    # User A watched ep 1, user B watched ep 2.
    session.add(
        UserEpisodeWatch(
            user_id=user_a.id,
            episode_id=show.id * 100 + 1,
            watched_at=datetime.now(UTC),
        )
    )
    session.add(
        UserEpisodeWatch(
            user_id=user_b.id,
            episode_id=show.id * 100 + 2,
            watched_at=datetime.now(UTC),
        )
    )
    await session.commit()

    candidates = [show.id * 100 + 1, show.id * 100 + 2]
    a_watched = await episode_watch_repo.watched_in(
        session, user_id=user_a.id, episode_ids=candidates
    )
    b_watched = await episode_watch_repo.watched_in(
        session, user_id=user_b.id, episode_ids=candidates
    )
    assert a_watched == {show.id * 100 + 1}
    assert b_watched == {show.id * 100 + 2}
