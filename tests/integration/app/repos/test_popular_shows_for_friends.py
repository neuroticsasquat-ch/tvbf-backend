"""Repo-level cases for the Popular with Friends ranking (NEU-1498, project spec §5.1, §8)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from tvbf.app.models import ActivityEvent, UserShowWatch
from tvbf.app.repos import activity_event_repo
from tvbf.app.repos.activity_repo import (
    POPULAR_LIMIT,
    POPULAR_WINDOW_DAYS,
    PopularShowRow,
    popular_shows_for_friends,
)
from tvbf.catalog.models import Episode, Show


async def _seed_show(session, show_id: int, **kwargs) -> Show:
    show = Show(id=show_id, name=f"Show-{show_id}", status="Ended", **kwargs)
    session.add(show)
    await session.flush()
    return show


async def _seed_episode(session, *, episode_id: int, show_id: int) -> Episode:
    ep = Episode(id=episode_id, show_id=show_id, season_number=1, episode_number=episode_id)
    session.add(ep)
    await session.flush()
    return ep


def _event(
    session,
    *,
    actor,
    verb: str = "added_show",
    target_type: str = "show",
    target_id: int,
    season_number: int | None = None,
    ago: timedelta = timedelta(hours=1),
) -> None:
    session.add(
        ActivityEvent(
            id=uuid4(),
            actor_id=actor.id,
            verb=verb,
            target_type=target_type,
            target_id=target_id,
            season_number=season_number,
            created_at=datetime.now(UTC) - ago,
        )
    )


async def _friends(make_user, n: int):
    return [await make_user(email=f"friend{i}@example.com") for i in range(n)]


def _ids(rows: list[PopularShowRow]) -> list[int]:
    return [r.show_id for r in rows]


def test_constants_match_the_spec():
    assert POPULAR_WINDOW_DAYS == 14
    assert POPULAR_LIMIT == 24


@pytest.mark.asyncio
async def test_empty_friend_ids_returns_empty_list(session):
    assert await popular_shows_for_friends(session, friend_ids=[]) == []


@pytest.mark.asyncio
async def test_distinct_friends_outrank_volume(session, make_user):
    a, b = await _friends(make_user, 2)
    await _seed_show(session, 1)
    await _seed_show(session, 2)
    # Show 1: two friends, one activity each.
    _event(session, actor=a, target_id=1)
    _event(session, actor=b, target_id=1)
    # Show 2: one friend, three activities.
    _event(session, actor=a, verb="added_show", target_id=2)
    _event(session, actor=a, verb="rated_show", target_id=2)
    _event(session, actor=a, verb="watched_show", target_id=2)
    await session.commit()

    rows = await popular_shows_for_friends(session, friend_ids=[a.id, b.id])

    assert rows == [
        PopularShowRow(show_id=1, friend_count=2),
        PopularShowRow(show_id=2, friend_count=1),
    ]


@pytest.mark.asyncio
async def test_tie_break_is_activity_count_then_recency_then_id_and_stable(session, make_user):
    (a,) = await _friends(make_user, 1)
    for show_id in (10, 20, 30, 40):
        await _seed_show(session, show_id)
    same = timedelta(hours=5)
    # 40: two activities — wins on count despite being oldest.
    _event(session, actor=a, verb="added_show", target_id=40, ago=timedelta(days=3))
    _event(session, actor=a, verb="rated_show", target_id=40, ago=timedelta(days=3))
    # 30: one activity, most recent.
    _event(session, actor=a, target_id=30, ago=timedelta(hours=1))
    # 10 and 20: one activity each at the same instant — the id decides.
    now = datetime.now(UTC)
    for show_id in (20, 10):
        session.add(
            ActivityEvent(
                id=uuid4(),
                actor_id=a.id,
                verb="added_show",
                target_type="show",
                target_id=show_id,
                created_at=now - same,
            )
        )
    await session.commit()

    first = await popular_shows_for_friends(session, friend_ids=[a.id])
    second = await popular_shows_for_friends(session, friend_ids=[a.id])

    assert _ids(first) == [40, 30, 10, 20]
    assert first == second


@pytest.mark.asyncio
async def test_window_edge(session, make_user):
    (a,) = await _friends(make_user, 1)
    await _seed_show(session, 1)
    await _seed_show(session, 2)
    _event(session, actor=a, target_id=1, ago=timedelta(days=13))
    _event(session, actor=a, target_id=2, ago=timedelta(days=15))
    await session.commit()

    assert _ids(await popular_shows_for_friends(session, friend_ids=[a.id])) == [1]


@pytest.mark.asyncio
async def test_re_emitted_activity_counts_at_its_new_timestamp(session, make_user):
    (a,) = await _friends(make_user, 1)
    await _seed_show(session, 1)
    _event(session, actor=a, target_id=1, ago=timedelta(days=30))
    await session.commit()
    assert await popular_shows_for_friends(session, friend_ids=[a.id]) == []

    await activity_event_repo.upsert(
        session, actor_id=a.id, verb="added_show", target_type="show", target_id=1
    )
    await session.commit()

    assert _ids(await popular_shows_for_friends(session, friend_ids=[a.id])) == [1]


@pytest.mark.asyncio
async def test_six_verbs_from_one_friend_count_once(session, make_user):
    (a,) = await _friends(make_user, 1)
    await _seed_show(session, 1)
    await _seed_episode(session, episode_id=101, show_id=1)
    _event(session, actor=a, verb="added_show", target_id=1)
    _event(session, actor=a, verb="watched_season", target_id=1, season_number=1)
    _event(session, actor=a, verb="watched_show", target_id=1)
    _event(session, actor=a, verb="rated_show", target_id=1)
    _event(session, actor=a, verb="watched_episode", target_type="episode", target_id=101)
    _event(session, actor=a, verb="rated_episode", target_type="episode", target_id=101)
    await session.commit()

    assert await popular_shows_for_friends(session, friend_ids=[a.id]) == [
        PopularShowRow(show_id=1, friend_count=1)
    ]


@pytest.mark.asyncio
async def test_global_switch_removes_the_friend_entirely(session, make_user):
    a, b = await _friends(make_user, 2)
    await _seed_show(session, 1)
    await _seed_show(session, 2)
    _event(session, actor=a, target_id=1)
    _event(session, actor=b, target_id=1)
    _event(session, actor=b, target_id=2)
    b.activity_feed_enabled = False
    await session.commit()

    assert await popular_shows_for_friends(session, friend_ids=[a.id, b.id]) == [
        PopularShowRow(show_id=1, friend_count=1)
    ]


@pytest.mark.asyncio
async def test_per_show_switch_removes_only_that_show(session, make_user):
    a, b = await _friends(make_user, 2)
    await _seed_show(session, 1)
    await _seed_show(session, 2)
    await _seed_episode(session, episode_id=201, show_id=2)
    _event(session, actor=a, target_id=1)
    _event(session, actor=b, target_id=1)
    # b's episode activity on show 2 resolves through the episode, then is hidden.
    _event(session, actor=b, verb="watched_episode", target_type="episode", target_id=201)
    session.add(UserShowWatch(user_id=b.id, show_id=1, hide_from_activity=True))
    session.add(UserShowWatch(user_id=b.id, show_id=2, hide_from_activity=False))
    await session.commit()

    assert set(await popular_shows_for_friends(session, friend_ids=[a.id, b.id])) == {
        PopularShowRow(show_id=1, friend_count=1),
        PopularShowRow(show_id=2, friend_count=1),
    }

    usw = await session.get(UserShowWatch, (b.id, 2))
    assert usw is not None
    usw.hide_from_activity = True
    await session.commit()

    assert await popular_shows_for_friends(session, friend_ids=[a.id, b.id]) == [
        PopularShowRow(show_id=1, friend_count=1)
    ]


@pytest.mark.asyncio
async def test_deleted_episode_activity_is_dropped(session, make_user):
    (a,) = await _friends(make_user, 1)
    await _seed_show(session, 1)
    _event(session, actor=a, verb="watched_episode", target_type="episode", target_id=999_999)
    await session.commit()

    assert await popular_shows_for_friends(session, friend_ids=[a.id]) == []


@pytest.mark.asyncio
async def test_adult_and_tombstoned_shows_are_absent(session, make_user):
    (a,) = await _friends(make_user, 1)
    await _seed_show(session, 1)
    await _seed_show(session, 2, adult=True)
    await _seed_show(session, 3, deleted_upstream_at=datetime.now(UTC))
    for show_id in (1, 2, 3):
        _event(session, actor=a, target_id=show_id)
    await session.commit()

    assert _ids(await popular_shows_for_friends(session, friend_ids=[a.id])) == [1]


@pytest.mark.asyncio
async def test_only_the_given_actors_count(session, make_user):
    a, outsider = await _friends(make_user, 2)
    await _seed_show(session, 1)
    await _seed_show(session, 2)
    _event(session, actor=a, target_id=1)
    _event(session, actor=outsider, target_id=2)
    await session.commit()

    assert _ids(await popular_shows_for_friends(session, friend_ids=[a.id])) == [1]


@pytest.mark.asyncio
async def test_limit_caps_the_list(session, make_user):
    (a,) = await _friends(make_user, 1)
    for show_id in range(1, 5):
        await _seed_show(session, show_id)
        _event(session, actor=a, target_id=show_id)
    await session.commit()

    assert len(await popular_shows_for_friends(session, friend_ids=[a.id], limit=2)) == 2


@pytest.mark.asyncio
async def test_a_verb_the_feed_does_not_show_is_not_counted(session, make_user):
    (a,) = await _friends(make_user, 1)
    await _seed_show(session, 1)
    _event(session, actor=a, verb="some_future_verb", target_id=1)
    await session.commit()

    assert await popular_shows_for_friends(session, friend_ids=[a.id]) == []
