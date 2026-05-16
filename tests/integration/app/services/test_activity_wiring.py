"""Integration tests that the /me mutation services emit/cancel activity events
in the same transaction as the underlying mutation (NEU-174)."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from tvbf.app.models import ActivityEvent
from tvbf.app.services import episode_service, my_shows_service, rating_service
from tvbf.tvmaze.models import Episode, Show


async def _seed_show(session, *, show_id: int, episodes_per_season=(2,)) -> Show:
    show = Show(id=show_id, name="A", tvmaze_updated=1, status="Ended")
    session.add(show)
    await session.flush()
    today = date.today()
    for season_idx, n in enumerate(episodes_per_season, start=1):
        for ep_num in range(1, n + 1):
            session.add(
                Episode(
                    id=show_id * 100 + season_idx * 10 + ep_num,
                    show_id=show.id,
                    season=season_idx,
                    number=ep_num,
                    airdate=today - timedelta(days=10),
                )
            )
    await session.flush()
    return show


async def _events(session, actor_id):
    rows = (
        (
            await session.execute(
                select(ActivityEvent)
                .where(ActivityEvent.actor_id == actor_id)
                .order_by(ActivityEvent.created_at)
            )
        )
        .scalars()
        .all()
    )
    return [(r.verb, r.target_type, r.target_id, r.season_number, r.payload) for r in rows]


@pytest.mark.asyncio
async def test_add_show_emits_added_show(session, make_user):
    user = await make_user(email="aw1@example.com")
    show = await _seed_show(session, show_id=8840001)
    await session.commit()

    await my_shows_service.add(session, user_id=user.id, show_id=show.id)
    assert await _events(session, user.id) == [("added_show", "show", show.id, None, None)]


@pytest.mark.asyncio
async def test_remove_show_cancels_added_show(session, make_user):
    user = await make_user(email="aw2@example.com")
    show = await _seed_show(session, show_id=8840002)
    await session.commit()

    await my_shows_service.add(session, user_id=user.id, show_id=show.id)
    await my_shows_service.remove(session, user_id=user.id, show_id=show.id)
    assert await _events(session, user.id) == []


@pytest.mark.asyncio
async def test_re_add_show_keeps_single_row(session, make_user):
    user = await make_user(email="aw2b@example.com")
    show = await _seed_show(session, show_id=8840003)
    await session.commit()

    await my_shows_service.add(session, user_id=user.id, show_id=show.id)
    await my_shows_service.add(session, user_id=user.id, show_id=show.id)
    rows = await _events(session, user.id)
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_mark_episode_emits_and_unmark_cancels(session, make_user):
    user = await make_user(email="aw3@example.com")
    show = await _seed_show(session, show_id=8840010)
    ep_id = show.id * 100 + 10 + 1
    await session.commit()

    await episode_service.mark_episode(session, user_id=user.id, episode_id=ep_id)
    assert await _events(session, user.id) == [("watched_episode", "episode", ep_id, None, None)]

    await episode_service.unmark_episode(session, user_id=user.id, episode_id=ep_id)
    assert await _events(session, user.id) == []


@pytest.mark.asyncio
async def test_bulk_mark_season_collapses_prior_episode_events(session, make_user):
    user = await make_user(email="aw4@example.com")
    show = await _seed_show(session, show_id=8840020, episodes_per_season=(2, 1))
    ep_s1_e1 = show.id * 100 + 10 + 1
    ep_s1_e2 = show.id * 100 + 10 + 2
    ep_s2_e1 = show.id * 100 + 20 + 1
    await session.commit()

    await episode_service.mark_episode(session, user_id=user.id, episode_id=ep_s1_e1)
    await episode_service.mark_episode(session, user_id=user.id, episode_id=ep_s2_e1)

    await episode_service.bulk_mark_season(
        session, user_id=user.id, show_id=show.id, season_number=1
    )

    rows = await _events(session, user.id)
    verbs = {(r[0], r[1], r[2], r[3]) for r in rows}
    # season 1 per-episode events collapsed away; season-2 episode survives;
    # one watched_season row for show + s1.
    assert ("watched_episode", "episode", ep_s1_e1, None) not in verbs
    assert ("watched_episode", "episode", ep_s1_e2, None) not in verbs
    assert ("watched_episode", "episode", ep_s2_e1, None) in verbs
    assert ("watched_season", "show", show.id, 1) in verbs


@pytest.mark.asyncio
async def test_bulk_unmark_season_cancels_season_event(session, make_user):
    user = await make_user(email="aw5@example.com")
    show = await _seed_show(session, show_id=8840030)
    await session.commit()

    await episode_service.bulk_mark_season(
        session, user_id=user.id, show_id=show.id, season_number=1
    )
    await episode_service.bulk_unmark_season(
        session, user_id=user.id, show_id=show.id, season_number=1
    )
    assert await _events(session, user.id) == []


@pytest.mark.asyncio
async def test_bulk_mark_show_collapses_prior_season_and_episode_events(session, make_user):
    user = await make_user(email="aw6@example.com")
    show = await _seed_show(session, show_id=8840040, episodes_per_season=(2, 1))
    ep_s2_e1 = show.id * 100 + 20 + 1
    await session.commit()

    await episode_service.bulk_mark_season(
        session, user_id=user.id, show_id=show.id, season_number=1
    )
    await episode_service.mark_episode(session, user_id=user.id, episode_id=ep_s2_e1)

    await episode_service.bulk_mark_show(session, user_id=user.id, show_id=show.id)

    rows = await _events(session, user.id)
    keyset = {(r[0], r[1], r[2], r[3]) for r in rows}
    assert keyset == {("watched_show", "show", show.id, None)}


@pytest.mark.asyncio
async def test_bulk_unmark_show_cancels_show_event(session, make_user):
    user = await make_user(email="aw7@example.com")
    show = await _seed_show(session, show_id=8840050)
    await session.commit()

    await episode_service.bulk_mark_show(session, user_id=user.id, show_id=show.id)
    await episode_service.bulk_unmark_show(session, user_id=user.id, show_id=show.id)
    assert await _events(session, user.id) == []


@pytest.mark.asyncio
async def test_set_show_rating_emits_with_payload(session, make_user):
    user = await make_user(email="aw8@example.com")
    show = await _seed_show(session, show_id=8840060)
    await session.commit()

    await rating_service.set_show_rating(
        session, user_id=user.id, show_id=show.id, stars=Decimal("4.5")
    )
    rows = await _events(session, user.id)
    assert rows == [("rated_show", "show", show.id, None, {"stars": 4.5})]


@pytest.mark.asyncio
async def test_re_rate_show_updates_payload_single_row(session, make_user):
    user = await make_user(email="aw9@example.com")
    show = await _seed_show(session, show_id=8840070)
    await session.commit()

    await rating_service.set_show_rating(
        session, user_id=user.id, show_id=show.id, stars=Decimal("3.0")
    )
    await rating_service.set_show_rating(
        session, user_id=user.id, show_id=show.id, stars=Decimal("4.5")
    )
    rows = await _events(session, user.id)
    assert rows == [("rated_show", "show", show.id, None, {"stars": 4.5})]


@pytest.mark.asyncio
async def test_clear_show_rating_cancels(session, make_user):
    user = await make_user(email="aw10@example.com")
    show = await _seed_show(session, show_id=8840080)
    await session.commit()

    await rating_service.set_show_rating(
        session, user_id=user.id, show_id=show.id, stars=Decimal("3.0")
    )
    await rating_service.clear_show_rating(session, user_id=user.id, show_id=show.id)
    assert await _events(session, user.id) == []


@pytest.mark.asyncio
async def test_set_and_clear_episode_rating(session, make_user):
    user = await make_user(email="aw11@example.com")
    show = await _seed_show(session, show_id=8840090)
    ep_id = show.id * 100 + 10 + 1
    await session.commit()

    await rating_service.set_episode_rating(
        session, user_id=user.id, episode_id=ep_id, stars=Decimal("5.0")
    )
    rows = await _events(session, user.id)
    assert rows == [("rated_episode", "episode", ep_id, None, {"stars": 5.0})]

    await rating_service.clear_episode_rating(session, user_id=user.id, episode_id=ep_id)
    assert await _events(session, user.id) == []
