from datetime import date, timedelta

import pytest

from tvbf.app.models import UserEpisodeWatch, UserShowWatch
from tvbf.app.repos import episode_repo
from tvbf.app.services import my_shows_service
from tvbf.tvmaze.models import Episode, Show


async def _seed_show(
    session,
    *,
    show_id: int,
    name: str = "S",
    episodes: int = 3,
    airdates: list[date] | None = None,
) -> Show:
    """Seed a show with `episodes` episodes in season 1. Each episode's airdate
    is taken from `airdates` if provided, else falls back to a date `i` days ago."""
    show = Show(id=show_id, name=name, tvmaze_updated=1)
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


@pytest.mark.asyncio
async def test_add_show_unknown_show_raises_not_found(session, make_user):
    from tvbf.app.errors import NotFound

    user = await make_user()
    await session.commit()
    with pytest.raises(NotFound):
        await my_shows_service.add(session, user_id=user.id, show_id=999_999_999)


@pytest.mark.asyncio
async def test_list_my_shows_empty_for_user_with_no_memberships(session, make_user):
    user = await make_user()
    await session.commit()
    assert await my_shows_service.list_my_shows(session, user_id=user.id) == []


@pytest.mark.asyncio
async def test_list_watch_next_empty_for_user_with_no_memberships(session, make_user):
    user = await make_user()
    await session.commit()
    assert await my_shows_service.list_watch_next(session, user_id=user.id) == []


@pytest.mark.asyncio
async def test_list_upcoming_empty_for_user_with_no_memberships(session, make_user):
    user = await make_user()
    await session.commit()
    assert await my_shows_service.list_upcoming(session, user_id=user.id) == []


@pytest.mark.asyncio
async def test_add_show_inserts(session, make_user):
    user = await make_user()
    show = await _seed_show(session, show_id=910001)
    await session.commit()

    await my_shows_service.add(session, user_id=user.id, show_id=show.id)
    await session.commit()

    rows = (
        await session.execute(
            UserShowWatch.__table__.select().where(UserShowWatch.user_id == user.id)
        )
    ).all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_add_show_idempotent(session, make_user):
    user = await make_user()
    show = await _seed_show(session, show_id=910002)
    await session.commit()

    await my_shows_service.add(session, user_id=user.id, show_id=show.id)
    await session.commit()
    await my_shows_service.add(session, user_id=user.id, show_id=show.id)
    await session.commit()

    rows = (
        await session.execute(
            UserShowWatch.__table__.select().where(UserShowWatch.user_id == user.id)
        )
    ).all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_remove_show_preserves_episode_history(session, make_user):
    user = await make_user()
    show = await _seed_show(session, show_id=910003)
    await session.commit()

    await my_shows_service.add(session, user_id=user.id, show_id=show.id)
    session.add(UserEpisodeWatch(user_id=user.id, episode_id=910003 * 100 + 1))
    await session.commit()

    await my_shows_service.remove(session, user_id=user.id, show_id=show.id)
    await session.commit()

    show_rows = (
        await session.execute(
            UserShowWatch.__table__.select().where(UserShowWatch.user_id == user.id)
        )
    ).all()
    ep_rows = (
        await session.execute(
            UserEpisodeWatch.__table__.select().where(UserEpisodeWatch.user_id == user.id)
        )
    ).all()
    assert show_rows == []
    assert len(ep_rows) == 1


@pytest.mark.asyncio
async def test_next_unwatched_episode(session, make_user):
    user = await make_user()
    show = await _seed_show(session, show_id=910004, episodes=3)
    await session.commit()

    nxt = await episode_repo.next_unwatched(session, user_id=user.id, show_id=show.id)
    assert nxt is not None and nxt.number == 1

    session.add(UserEpisodeWatch(user_id=user.id, episode_id=910004 * 100 + 1))
    await session.commit()
    nxt = await episode_repo.next_unwatched(session, user_id=user.id, show_id=show.id)
    assert nxt is not None and nxt.number == 2

    session.add(UserEpisodeWatch(user_id=user.id, episode_id=910004 * 100 + 2))
    session.add(UserEpisodeWatch(user_id=user.id, episode_id=910004 * 100 + 3))
    await session.commit()
    nxt = await episode_repo.next_unwatched(session, user_id=user.id, show_id=show.id)
    assert nxt is None


@pytest.mark.asyncio
async def test_list_my_shows_returns_tracked_only(session, make_user):
    user = await make_user()
    a = await _seed_show(session, show_id=910005, name="A", episodes=2)
    b = await _seed_show(session, show_id=910006, name="B", episodes=2)
    await session.commit()
    await my_shows_service.add(session, user_id=user.id, show_id=a.id)
    await session.commit()

    rows = await my_shows_service.list_my_shows(session, user_id=user.id)
    ids = [e.show.id for e in rows]
    assert ids == [a.id]
    assert b.id not in ids


@pytest.mark.asyncio
async def test_list_my_shows_includes_counts_and_next(session, make_user):
    user = await make_user()
    show = await _seed_show(session, show_id=910007, name="W", episodes=3)
    await session.commit()
    await my_shows_service.add(session, user_id=user.id, show_id=show.id)
    session.add(UserEpisodeWatch(user_id=user.id, episode_id=910007 * 100 + 1))
    await session.commit()

    rows = await my_shows_service.list_my_shows(session, user_id=user.id)
    assert len(rows) == 1
    e = rows[0]
    assert e.watched_episode_count == 1
    assert e.total_episode_count == 3
    assert e.next_episode is not None
    assert e.next_episode.number == 2


@pytest.mark.asyncio
async def test_list_my_shows_sort_name(session, make_user):
    user = await make_user()
    today = date.today()
    a = await _seed_show(
        session, show_id=910010, name="Charlie", airdates=[today - timedelta(days=1)]
    )
    b = await _seed_show(
        session, show_id=910011, name="alpha", airdates=[today - timedelta(days=2)]
    )
    await session.commit()
    await my_shows_service.add(session, user_id=user.id, show_id=a.id)
    await my_shows_service.add(session, user_id=user.id, show_id=b.id)
    await session.commit()

    rows = await my_shows_service.list_my_shows(session, user_id=user.id, sort="name_asc")
    assert [e.show.id for e in rows] == [b.id, a.id]


@pytest.mark.asyncio
async def test_watch_next_excludes_unaired_and_watched(session, make_user):
    user = await make_user()
    today = date.today()
    show = Show(id=910020, name="Wnext", tvmaze_updated=1)
    session.add(show)
    await session.flush()
    # ep 1 aired yesterday, watched
    session.add(
        Episode(id=91002001, show_id=show.id, season=1, number=1, airdate=today - timedelta(days=1))
    )
    # ep 2 aired today, unwatched (the one we expect)
    session.add(Episode(id=91002002, show_id=show.id, season=1, number=2, airdate=today))
    # ep 3 airs tomorrow, unwatched but not yet aired
    session.add(
        Episode(id=91002003, show_id=show.id, season=1, number=3, airdate=today + timedelta(days=1))
    )
    await session.flush()
    await my_shows_service.add(session, user_id=user.id, show_id=show.id)
    session.add(UserEpisodeWatch(user_id=user.id, episode_id=91002001))
    await session.commit()

    rows = await my_shows_service.list_watch_next(session, user_id=user.id)
    assert len(rows) == 1
    assert rows[0].show.id == show.id
    assert rows[0].episode.id == 91002002


@pytest.mark.asyncio
async def test_watch_next_skips_show_with_nothing_aired_unwatched(session, make_user):
    user = await make_user()
    today = date.today()
    show = Show(id=910030, name="Caught", tvmaze_updated=1)
    session.add(show)
    await session.flush()
    session.add(
        Episode(id=91003001, show_id=show.id, season=1, number=1, airdate=today - timedelta(days=2))
    )
    await session.flush()
    await my_shows_service.add(session, user_id=user.id, show_id=show.id)
    session.add(UserEpisodeWatch(user_id=user.id, episode_id=91003001))
    await session.commit()

    assert await my_shows_service.list_watch_next(session, user_id=user.id) == []


@pytest.mark.asyncio
async def test_watch_next_skips_show_not_in_my_shows(session, make_user):
    user = await make_user()
    today = date.today()
    show = Show(id=910040, name="Untracked", tvmaze_updated=1)
    session.add(show)
    await session.flush()
    session.add(
        Episode(id=91004001, show_id=show.id, season=1, number=1, airdate=today - timedelta(days=1))
    )
    await session.flush()
    # User has watched episodes but show isn't in My Shows.
    session.add(UserEpisodeWatch(user_id=user.id, episode_id=91004001))
    await session.commit()
    assert await my_shows_service.list_watch_next(session, user_id=user.id) == []


@pytest.mark.asyncio
async def test_watch_next_default_sort_airdate_desc(session, make_user):
    user = await make_user()
    today = date.today()
    a = Show(id=910050, name="Aaa", tvmaze_updated=1)
    b = Show(id=910051, name="Bbb", tvmaze_updated=1)
    session.add(a)
    session.add(b)
    await session.flush()
    session.add(
        Episode(id=91005001, show_id=a.id, season=1, number=1, airdate=today - timedelta(days=5))
    )
    session.add(
        Episode(id=91005101, show_id=b.id, season=1, number=1, airdate=today - timedelta(days=1))
    )
    await session.flush()
    await my_shows_service.add(session, user_id=user.id, show_id=a.id)
    await my_shows_service.add(session, user_id=user.id, show_id=b.id)
    await session.commit()

    rows = await my_shows_service.list_watch_next(session, user_id=user.id)
    assert [e.show.id for e in rows] == [b.id, a.id]


@pytest.mark.asyncio
async def test_upcoming_returns_only_future_episodes(session, make_user):
    user = await make_user()
    today = date.today()
    show = Show(id=910060, name="Future", tvmaze_updated=1)
    session.add(show)
    await session.flush()
    session.add(
        Episode(id=91006001, show_id=show.id, season=1, number=1, airdate=today - timedelta(days=1))
    )
    session.add(
        Episode(id=91006002, show_id=show.id, season=1, number=2, airdate=today + timedelta(days=2))
    )
    session.add(
        Episode(id=91006003, show_id=show.id, season=1, number=3, airdate=today + timedelta(days=9))
    )
    await session.flush()
    await my_shows_service.add(session, user_id=user.id, show_id=show.id)
    await session.commit()

    rows = await my_shows_service.list_upcoming(session, user_id=user.id)
    assert len(rows) == 1
    assert rows[0].episode.id == 91006002


@pytest.mark.asyncio
async def test_upcoming_default_sort_airdate_asc(session, make_user):
    user = await make_user()
    today = date.today()
    a = Show(id=910070, name="A", tvmaze_updated=1)
    b = Show(id=910071, name="B", tvmaze_updated=1)
    session.add(a)
    session.add(b)
    await session.flush()
    session.add(
        Episode(id=91007001, show_id=a.id, season=1, number=1, airdate=today + timedelta(days=10))
    )
    session.add(
        Episode(id=91007101, show_id=b.id, season=1, number=1, airdate=today + timedelta(days=2))
    )
    await session.flush()
    await my_shows_service.add(session, user_id=user.id, show_id=a.id)
    await my_shows_service.add(session, user_id=user.id, show_id=b.id)
    await session.commit()

    rows = await my_shows_service.list_upcoming(session, user_id=user.id)
    assert [e.show.id for e in rows] == [b.id, a.id]


@pytest.mark.asyncio
async def test_upcoming_skips_episodes_with_null_airdate(session, make_user):
    user = await make_user()
    show = Show(id=910080, name="Null", tvmaze_updated=1)
    session.add(show)
    await session.flush()
    session.add(Episode(id=91008001, show_id=show.id, season=1, number=1, airdate=None))
    await session.flush()
    await my_shows_service.add(session, user_id=user.id, show_id=show.id)
    await session.commit()

    assert await my_shows_service.list_upcoming(session, user_id=user.id) == []


@pytest.mark.asyncio
async def test_list_my_shows_includes_last_watched_at(session, make_user):
    user = await make_user()
    show = await _seed_show(session, show_id=910090, name="LW", episodes=3)
    await session.commit()
    await my_shows_service.add(session, user_id=user.id, show_id=show.id)
    session.add(UserEpisodeWatch(user_id=user.id, episode_id=910090 * 100 + 1))
    await session.commit()

    rows = await my_shows_service.list_my_shows(session, user_id=user.id)
    assert len(rows) == 1
    assert rows[0].last_watched_at is not None


@pytest.mark.asyncio
async def test_list_watch_next_uses_supplied_today_as_upper_bound(session, make_user):
    """An episode airing on the supplied `today` is included; airing the next day is not."""
    user = await make_user(email="wn-today@example.com")
    show = await _seed_show(
        session,
        show_id=950100,
        name="Today Show",
        episodes=2,
        airdates=[date(2026, 5, 6), date(2026, 5, 7)],
    )
    session.add(UserShowWatch(user_id=user.id, show_id=show.id))
    await session.commit()

    rows = await my_shows_service.list_watch_next(session, user_id=user.id, today=date(2026, 5, 6))
    assert [e.episode.id for e in rows] == [show.id * 100 + 1]

    rows = await my_shows_service.list_watch_next(session, user_id=user.id, today=date(2026, 5, 5))
    assert rows == []


@pytest.mark.asyncio
async def test_list_upcoming_uses_supplied_today_as_lower_bound(session, make_user):
    """Episode airing on `today` is NOT upcoming; airing the next day IS."""
    user = await make_user(email="up-today@example.com")
    show = await _seed_show(
        session,
        show_id=950110,
        name="Upcoming Today",
        episodes=2,
        airdates=[date(2026, 5, 6), date(2026, 5, 7)],
    )
    session.add(UserShowWatch(user_id=user.id, show_id=show.id))
    await session.commit()

    rows = await my_shows_service.list_upcoming(session, user_id=user.id, today=date(2026, 5, 6))
    assert [e.episode.id for e in rows] == [show.id * 100 + 2]

    rows = await my_shows_service.list_upcoming(session, user_id=user.id, today=date(2026, 5, 7))
    assert rows == []


# ---------------------------------------------------------------------------
# NEU-100 Path B: EpisodeOut.watched populated on list endpoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_my_shows_next_episode_carries_watched_flag(session, make_user):
    """The next-episode included in `MyShowEntry` carries `watched=False` so
    the frontend can render the watch checkbox without a per-show round trip."""
    user = await make_user(email="me-watched@example.com")
    show = await _seed_show(session, show_id=910500, episodes=2)
    session.add(UserShowWatch(user_id=user.id, show_id=show.id))
    await session.commit()

    rows = await my_shows_service.list_my_shows(session, user_id=user.id, today=date.today())
    assert len(rows) == 1
    assert rows[0].next_episode is not None
    assert rows[0].next_episode.watched is False


@pytest.mark.asyncio
async def test_list_watch_next_episode_carries_watched_flag(session, make_user):
    user = await make_user(email="wn-watched@example.com")
    show = await _seed_show(session, show_id=910501, episodes=2)
    session.add(UserShowWatch(user_id=user.id, show_id=show.id))
    await session.commit()

    rows = await my_shows_service.list_watch_next(session, user_id=user.id, today=date.today())
    assert len(rows) >= 1
    assert all(e.episode.watched is False for e in rows)


@pytest.mark.asyncio
async def test_list_upcoming_episode_carries_watched_flag(session, make_user):
    """Future episodes are unwatched by definition; `watched` is still populated
    explicitly so the frontend doesn't have to special-case it."""
    user = await make_user(email="up-watched@example.com")
    future = date.today() + timedelta(days=1)
    show = await _seed_show(
        session,
        show_id=910502,
        episodes=1,
        airdates=[future],
    )
    session.add(UserShowWatch(user_id=user.id, show_id=show.id))
    await session.commit()

    rows = await my_shows_service.list_upcoming(session, user_id=user.id, today=date.today())
    assert len(rows) == 1
    assert rows[0].episode.watched is False
