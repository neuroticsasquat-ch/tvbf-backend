import asyncio

import pytest
from sqlalchemy import select

from tvbf.app.models import ActivityEvent
from tvbf.app.repos import activity_event_repo
from tvbf.tvmaze.models import Episode, Show


async def _seed_show(session, *, show_id: int = 8810001) -> Show:
    show = Show(id=show_id, name="A", tvmaze_updated=1, status="Ended")
    session.add(show)
    await session.flush()
    return show


async def _seed_episode(session, *, episode_id: int, show_id: int, season: int) -> Episode:
    ep = Episode(id=episode_id, show_id=show_id, season=season, number=1)
    session.add(ep)
    await session.flush()
    return ep


async def _count(session) -> int:
    rows = (await session.execute(select(ActivityEvent))).scalars().all()
    return len(rows)


@pytest.mark.asyncio
async def test_emit_then_cancel_returns_table_to_empty(session, make_user):
    user = await make_user(email="ae1@example.com")
    await session.commit()

    await activity_event_repo.upsert(
        session,
        actor_id=user.id,
        verb="added_show",
        target_type="show",
        target_id=1,
    )
    assert await _count(session) == 1

    deleted = await activity_event_repo.delete(
        session,
        actor_id=user.id,
        verb="added_show",
        target_type="show",
        target_id=1,
    )
    assert deleted == 1
    assert await _count(session) == 0


@pytest.mark.asyncio
async def test_double_emit_produces_one_row_with_updated_timestamp(session, make_user):
    user = await make_user(email="ae2@example.com")
    await session.commit()

    row1 = await activity_event_repo.upsert(
        session,
        actor_id=user.id,
        verb="watched_episode",
        target_type="episode",
        target_id=42,
    )
    first_ts = row1.created_at
    await session.commit()
    await asyncio.sleep(0.01)

    row2 = await activity_event_repo.upsert(
        session,
        actor_id=user.id,
        verb="watched_episode",
        target_type="episode",
        target_id=42,
    )
    await session.commit()

    assert row1.id == row2.id
    assert row2.created_at >= first_ts
    assert await _count(session) == 1


@pytest.mark.asyncio
async def test_double_cancel_is_idempotent(session, make_user):
    user = await make_user(email="ae3@example.com")
    await session.commit()

    first = await activity_event_repo.delete(
        session,
        actor_id=user.id,
        verb="added_show",
        target_type="show",
        target_id=99,
    )
    second = await activity_event_repo.delete(
        session,
        actor_id=user.id,
        verb="added_show",
        target_type="show",
        target_id=99,
    )
    assert first == 0
    assert second == 0


@pytest.mark.asyncio
async def test_rated_show_upsert_updates_payload(session, make_user):
    user = await make_user(email="ae4@example.com")
    await session.commit()

    await activity_event_repo.upsert(
        session,
        actor_id=user.id,
        verb="rated_show",
        target_type="show",
        target_id=7,
        payload={"stars": 3.0},
    )
    await activity_event_repo.upsert(
        session,
        actor_id=user.id,
        verb="rated_show",
        target_type="show",
        target_id=7,
        payload={"stars": 4.5},
    )
    row = (
        await session.execute(
            select(ActivityEvent)
            .where(ActivityEvent.actor_id == user.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert row.payload == {"stars": 4.5}
    assert await _count(session) == 1


@pytest.mark.asyncio
async def test_season_event_uses_season_number_for_uniqueness(session, make_user):
    user = await make_user(email="ae5@example.com")
    await session.commit()

    await activity_event_repo.upsert(
        session,
        actor_id=user.id,
        verb="watched_season",
        target_type="show",
        target_id=10,
        season_number=1,
    )
    await activity_event_repo.upsert(
        session,
        actor_id=user.id,
        verb="watched_season",
        target_type="show",
        target_id=10,
        season_number=2,
    )
    assert await _count(session) == 2

    # canceling season 1 leaves season 2 in place
    await activity_event_repo.delete(
        session,
        actor_id=user.id,
        verb="watched_season",
        target_type="show",
        target_id=10,
        season_number=1,
    )
    rows = (await session.execute(select(ActivityEvent))).scalars().all()
    assert len(rows) == 1
    assert rows[0].season_number == 2


@pytest.mark.asyncio
async def test_delete_episode_events_for_season_scoped(session, make_user):
    user = await make_user(email="ae6@example.com")
    other = await make_user(email="ae6b@example.com")
    show = await _seed_show(session, show_id=8810010)
    other_show = await _seed_show(session, show_id=8810011)

    e1 = await _seed_episode(session, episode_id=9001, show_id=show.id, season=1)
    e2 = await _seed_episode(session, episode_id=9002, show_id=show.id, season=1)
    e3 = await _seed_episode(session, episode_id=9003, show_id=show.id, season=2)
    e4 = await _seed_episode(session, episode_id=9004, show_id=other_show.id, season=1)
    await session.commit()

    for ep in (e1, e2, e3, e4):
        await activity_event_repo.upsert(
            session,
            actor_id=user.id,
            verb="watched_episode",
            target_type="episode",
            target_id=ep.id,
        )
    # other actor's S1 ep on same show should be untouched
    await activity_event_repo.upsert(
        session,
        actor_id=other.id,
        verb="watched_episode",
        target_type="episode",
        target_id=e1.id,
    )
    await session.commit()

    deleted = await activity_event_repo.delete_episode_events_for_season(
        session, actor_id=user.id, show_id=show.id, season_number=1
    )
    assert deleted == 2

    remaining = {
        (r.actor_id, r.target_id)
        for r in (await session.execute(select(ActivityEvent))).scalars().all()
    }
    assert remaining == {
        (user.id, e3.id),  # S2 same show
        (user.id, e4.id),  # other show
        (other.id, e1.id),  # other actor
    }


@pytest.mark.asyncio
async def test_delete_episode_and_season_events_for_show(session, make_user):
    user = await make_user(email="ae7@example.com")
    show = await _seed_show(session, show_id=8810020)
    other_show = await _seed_show(session, show_id=8810021)
    e1 = await _seed_episode(session, episode_id=9101, show_id=show.id, season=1)
    e2 = await _seed_episode(session, episode_id=9102, show_id=show.id, season=2)
    e3 = await _seed_episode(session, episode_id=9103, show_id=other_show.id, season=1)
    await session.commit()

    for ep in (e1, e2, e3):
        await activity_event_repo.upsert(
            session,
            actor_id=user.id,
            verb="watched_episode",
            target_type="episode",
            target_id=ep.id,
        )
    await activity_event_repo.upsert(
        session,
        actor_id=user.id,
        verb="watched_season",
        target_type="show",
        target_id=show.id,
        season_number=1,
    )
    await activity_event_repo.upsert(
        session,
        actor_id=user.id,
        verb="watched_season",
        target_type="show",
        target_id=other_show.id,
        season_number=1,
    )
    await session.commit()

    deleted = await activity_event_repo.delete_episode_and_season_events_for_show(
        session, actor_id=user.id, show_id=show.id
    )
    # 2 episode events on this show + 1 season event for this show
    assert deleted == 3

    remaining = {
        (r.verb, r.target_id, r.season_number)
        for r in (await session.execute(select(ActivityEvent))).scalars().all()
    }
    assert remaining == {
        ("watched_episode", e3.id, None),
        ("watched_season", other_show.id, 1),
    }
