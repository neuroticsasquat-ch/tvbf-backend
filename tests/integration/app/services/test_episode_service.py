"""Direct integration tests for episode_service.

Mirrors the my_shows_service test pattern. Covers the same paths as
test_me_routes.py for the episode endpoints, with finer assertions and direct
service-layer execution that coverage can trace.
"""

import pytest

from tvbf.app.errors import NotFound
from tvbf.app.services import episode_service
from tvbf.tvmaze.models import Episode, Show


async def _seed_show(
    session, *, show_id: int, episodes_per_season: dict[int, int] | None = None
) -> Show:
    """Seed a show with episodes. By default 3 episodes in season 1."""
    show = Show(id=show_id, name=f"Show{show_id}", tvmaze_updated=1)
    session.add(show)
    await session.flush()
    seasons = episodes_per_season or {1: 3}
    ep_id = show_id * 1000
    for season, count in seasons.items():
        for n in range(1, count + 1):
            ep_id += 1
            session.add(Episode(id=ep_id, show_id=show.id, season=season, number=n))
    await session.flush()
    return show


# ---------------------------------------------------------------------------
# mark_episode / unmark_episode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mark_episode_creates_watch_row(session, make_user):
    user = await make_user()
    await _seed_show(session, show_id=950001)
    await session.commit()
    ep_id = 950001 * 1000 + 1

    out = await episode_service.mark_episode(session, user_id=user.id, episode_id=ep_id)
    assert out.episode_id == ep_id
    assert out.watched_at is not None

    ids = await episode_service.list_watched_episode_ids(session, user_id=user.id, show_id=950001)
    assert ids == [ep_id]


@pytest.mark.asyncio
async def test_mark_episode_idempotent(session, make_user):
    user = await make_user()
    await _seed_show(session, show_id=950002)
    await session.commit()
    ep_id = 950002 * 1000 + 1

    first = await episode_service.mark_episode(session, user_id=user.id, episode_id=ep_id)
    second = await episode_service.mark_episode(session, user_id=user.id, episode_id=ep_id)
    # Second call must not change watched_at — the existing row's timestamp
    # is preserved.
    assert second.watched_at == first.watched_at


@pytest.mark.asyncio
async def test_mark_episode_unknown_raises_not_found(session, make_user):
    user = await make_user()
    with pytest.raises(NotFound):
        await episode_service.mark_episode(session, user_id=user.id, episode_id=999999999)


@pytest.mark.asyncio
async def test_unmark_episode_removes_watch_row(session, make_user):
    user = await make_user()
    await _seed_show(session, show_id=950003)
    await session.commit()
    ep_id = 950003 * 1000 + 1
    await episode_service.mark_episode(session, user_id=user.id, episode_id=ep_id)

    await episode_service.unmark_episode(session, user_id=user.id, episode_id=ep_id)

    ids = await episode_service.list_watched_episode_ids(session, user_id=user.id, show_id=950003)
    assert ids == []


@pytest.mark.asyncio
async def test_unmark_episode_idempotent_for_unmarked(session, make_user):
    user = await make_user()
    await _seed_show(session, show_id=950004)
    await session.commit()
    # Unmarking a never-watched episode is a no-op (no exception).
    await episode_service.unmark_episode(session, user_id=user.id, episode_id=950004 * 1000 + 1)


# ---------------------------------------------------------------------------
# list_watched_episode_ids
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_watched_episode_ids_only_returns_for_requested_show(session, make_user):
    user = await make_user()
    await _seed_show(session, show_id=950010)
    await _seed_show(session, show_id=950011)
    await session.commit()

    await episode_service.mark_episode(session, user_id=user.id, episode_id=950010 * 1000 + 1)
    await episode_service.mark_episode(session, user_id=user.id, episode_id=950011 * 1000 + 1)

    ids = await episode_service.list_watched_episode_ids(session, user_id=user.id, show_id=950010)
    assert ids == [950010 * 1000 + 1]


@pytest.mark.asyncio
async def test_list_watched_episode_ids_empty_when_no_watches(session, make_user):
    user = await make_user()
    await _seed_show(session, show_id=950020)
    await session.commit()
    ids = await episode_service.list_watched_episode_ids(session, user_id=user.id, show_id=950020)
    assert ids == []


# ---------------------------------------------------------------------------
# bulk_mark_season / bulk_unmark_season
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_mark_season_marks_all_episodes_in_season(session, make_user):
    user = await make_user()
    await _seed_show(session, show_id=950030, episodes_per_season={1: 3, 2: 2})
    await session.commit()

    marked = await episode_service.bulk_mark_season(
        session, user_id=user.id, show_id=950030, season_number=1
    )
    assert marked == 3

    ids = await episode_service.list_watched_episode_ids(session, user_id=user.id, show_id=950030)
    assert len(ids) == 3
    # Season 2 untouched.
    s1_ep_ids = {950030 * 1000 + i for i in (1, 2, 3)}
    assert set(ids) == s1_ep_ids


@pytest.mark.asyncio
async def test_bulk_mark_season_idempotent(session, make_user):
    user = await make_user()
    await _seed_show(session, show_id=950031, episodes_per_season={1: 3})
    await session.commit()

    first = await episode_service.bulk_mark_season(
        session, user_id=user.id, show_id=950031, season_number=1
    )
    second = await episode_service.bulk_mark_season(
        session, user_id=user.id, show_id=950031, season_number=1
    )
    assert first == 3 and second == 3

    ids = await episode_service.list_watched_episode_ids(session, user_id=user.id, show_id=950031)
    assert len(ids) == 3


@pytest.mark.asyncio
async def test_bulk_mark_unknown_season_raises_not_found(session, make_user):
    user = await make_user()
    await _seed_show(session, show_id=950032, episodes_per_season={1: 1})
    await session.commit()

    with pytest.raises(NotFound):
        await episode_service.bulk_mark_season(
            session, user_id=user.id, show_id=950032, season_number=99
        )


@pytest.mark.asyncio
async def test_bulk_mark_unknown_show_raises_not_found(session, make_user):
    user = await make_user()
    with pytest.raises(NotFound):
        await episode_service.bulk_mark_season(
            session, user_id=user.id, show_id=999999, season_number=1
        )


@pytest.mark.asyncio
async def test_bulk_unmark_season_removes_all_episodes_in_season(session, make_user):
    user = await make_user()
    await _seed_show(session, show_id=950040, episodes_per_season={1: 3, 2: 2})
    await session.commit()
    await episode_service.bulk_mark_season(
        session, user_id=user.id, show_id=950040, season_number=1
    )
    await episode_service.bulk_mark_season(
        session, user_id=user.id, show_id=950040, season_number=2
    )

    await episode_service.bulk_unmark_season(
        session, user_id=user.id, show_id=950040, season_number=1
    )

    ids = await episode_service.list_watched_episode_ids(session, user_id=user.id, show_id=950040)
    # Season 1 unmarked, season 2 still watched.
    assert sorted(ids) == sorted([950040 * 1000 + 4, 950040 * 1000 + 5])


@pytest.mark.asyncio
async def test_bulk_unmark_season_no_episodes_is_noop(session, make_user):
    user = await make_user()
    await _seed_show(session, show_id=950041, episodes_per_season={1: 1})
    await session.commit()
    # Unmarking an unknown season silently does nothing (mirrors route behavior).
    await episode_service.bulk_unmark_season(
        session, user_id=user.id, show_id=950041, season_number=99
    )
