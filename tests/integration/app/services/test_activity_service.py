import pytest
from sqlalchemy import select

from tvbf.app.models import ActivityEvent
from tvbf.app.services import activity_service
from tvbf.tvmaze.models import Episode, Show


async def _seed_show(session, *, show_id: int) -> Show:
    show = Show(id=show_id, name="A", tvmaze_updated=1, status="Ended")
    session.add(show)
    await session.flush()
    return show


async def _seed_episode(session, *, episode_id: int, show_id: int, season: int) -> Episode:
    ep = Episode(id=episode_id, show_id=show_id, season=season, number=1)
    session.add(ep)
    await session.flush()
    return ep


@pytest.mark.asyncio
async def test_emit_and_cancel_added_show(session, make_user):
    user = await make_user(email="as1@example.com")
    await session.commit()

    await activity_service.emit(
        session, actor_id=user.id, verb="added_show", target_type="show", target_id=5
    )
    rows = (await session.execute(select(ActivityEvent))).scalars().all()
    assert len(rows) == 1

    await activity_service.cancel(
        session, actor_id=user.id, verb="added_show", target_type="show", target_id=5
    )
    rows = (await session.execute(select(ActivityEvent))).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_collapse_for_season_scoped_to_actor_show_and_season(session, make_user):
    actor = await make_user(email="as2a@example.com")
    other_actor = await make_user(email="as2b@example.com")
    show = await _seed_show(session, show_id=8820010)
    other_show = await _seed_show(session, show_id=8820011)
    e_s1a = await _seed_episode(session, episode_id=9201, show_id=show.id, season=1)
    e_s1b = await _seed_episode(session, episode_id=9202, show_id=show.id, season=1)
    e_s2 = await _seed_episode(session, episode_id=9203, show_id=show.id, season=2)
    e_other_show = await _seed_episode(session, episode_id=9204, show_id=other_show.id, season=1)
    await session.commit()

    for ep in (e_s1a, e_s1b, e_s2, e_other_show):
        await activity_service.emit(
            session,
            actor_id=actor.id,
            verb="watched_episode",
            target_type="episode",
            target_id=ep.id,
        )
    await activity_service.emit(
        session,
        actor_id=other_actor.id,
        verb="watched_episode",
        target_type="episode",
        target_id=e_s1a.id,
    )
    await session.commit()

    await activity_service.collapse_for_season(
        session, actor_id=actor.id, show_id=show.id, season_number=1
    )

    remaining = {
        (r.actor_id, r.target_id)
        for r in (await session.execute(select(ActivityEvent))).scalars().all()
    }
    assert remaining == {
        (actor.id, e_s2.id),
        (actor.id, e_other_show.id),
        (other_actor.id, e_s1a.id),
    }


@pytest.mark.asyncio
async def test_collapse_for_show_removes_seasons_and_episodes_for_show(session, make_user):
    actor = await make_user(email="as3@example.com")
    show = await _seed_show(session, show_id=8830010)
    other_show = await _seed_show(session, show_id=8830011)
    e_s1 = await _seed_episode(session, episode_id=9301, show_id=show.id, season=1)
    e_other = await _seed_episode(session, episode_id=9302, show_id=other_show.id, season=1)
    await session.commit()

    await activity_service.emit(
        session,
        actor_id=actor.id,
        verb="watched_episode",
        target_type="episode",
        target_id=e_s1.id,
    )
    await activity_service.emit(
        session,
        actor_id=actor.id,
        verb="watched_season",
        target_type="show",
        target_id=show.id,
        season_number=1,
    )
    await activity_service.emit(
        session,
        actor_id=actor.id,
        verb="watched_episode",
        target_type="episode",
        target_id=e_other.id,
    )
    await session.commit()

    await activity_service.collapse_for_show(session, actor_id=actor.id, show_id=show.id)

    remaining = {
        (r.verb, r.target_id, r.season_number)
        for r in (await session.execute(select(ActivityEvent))).scalars().all()
    }
    # other show's episode survives
    assert remaining == {("watched_episode", e_other.id, None)}
