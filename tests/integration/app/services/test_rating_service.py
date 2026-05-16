"""Service-level tests for rating_service (NEU-166)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tvbf.app.errors import NotFound
from tvbf.app.repos import (
    episode_rating_repo,
    show_rating_repo,
)
from tvbf.app.services import connection_service, rating_service
from tvbf.tvmaze.models import Episode, Show


async def _seed_show(session, *, show_id: int = 8800001) -> Show:
    show = Show(id=show_id, name="ShowR", tvmaze_updated=1, status="Ended")
    session.add(show)
    await session.flush()
    return show


async def _seed_episode(session, *, show_id: int, episode_id: int) -> Episode:
    ep = Episode(id=episode_id, show_id=show_id, season=1, number=1)
    session.add(ep)
    await session.flush()
    return ep


async def _accept_pair(session, a, b) -> None:
    req = await connection_service.send_request(session, requester_id=a.id, addressee_id=b.id)
    await connection_service.accept(session, id=req.id, accepting_user_id=b.id)


@pytest.mark.asyncio
async def test_set_show_rating_upserts(session, make_user):
    user = await make_user(email="rs1@example.com")
    show = await _seed_show(session, show_id=8800101)
    await session.commit()

    out = await rating_service.set_show_rating(
        session, user_id=user.id, show_id=show.id, stars=Decimal("4.5")
    )
    assert out.stars == 4.5
    assert out.show_id == show.id

    out2 = await rating_service.set_show_rating(
        session, user_id=user.id, show_id=show.id, stars=Decimal("3.0")
    )
    assert out2.stars == 3.0


@pytest.mark.asyncio
async def test_set_show_rating_404(session, make_user):
    user = await make_user(email="rs2@example.com")
    await session.commit()
    with pytest.raises(NotFound):
        await rating_service.set_show_rating(
            session, user_id=user.id, show_id=9_999_999, stars=Decimal("3.0")
        )


@pytest.mark.asyncio
async def test_clear_show_rating_idempotent(session, make_user):
    user = await make_user(email="rs3@example.com")
    show = await _seed_show(session, show_id=8800102)
    await session.commit()
    await rating_service.set_show_rating(
        session, user_id=user.id, show_id=show.id, stars=Decimal("3.0")
    )
    assert await rating_service.clear_show_rating(session, user_id=user.id, show_id=show.id) == 1
    assert await rating_service.clear_show_rating(session, user_id=user.id, show_id=show.id) == 0


@pytest.mark.asyncio
async def test_set_episode_rating_upserts_and_404(session, make_user):
    user = await make_user(email="rs4@example.com")
    show = await _seed_show(session, show_id=8800103)
    ep = await _seed_episode(session, show_id=show.id, episode_id=88001031)
    await session.commit()

    out = await rating_service.set_episode_rating(
        session, user_id=user.id, episode_id=ep.id, stars=Decimal("2.5")
    )
    assert out.stars == 2.5
    assert out.episode_id == ep.id

    out2 = await rating_service.set_episode_rating(
        session, user_id=user.id, episode_id=ep.id, stars=Decimal("5.0")
    )
    assert out2.stars == 5.0

    with pytest.raises(NotFound):
        await rating_service.set_episode_rating(
            session, user_id=user.id, episode_id=9_999_999, stars=Decimal("3.0")
        )


@pytest.mark.asyncio
async def test_clear_episode_rating_idempotent(session, make_user):
    user = await make_user(email="rs5@example.com")
    show = await _seed_show(session, show_id=8800104)
    ep = await _seed_episode(session, show_id=show.id, episode_id=88001041)
    await session.commit()
    await rating_service.set_episode_rating(
        session, user_id=user.id, episode_id=ep.id, stars=Decimal("3.0")
    )
    assert (
        await rating_service.clear_episode_rating(session, user_id=user.id, episode_id=ep.id) == 1
    )
    assert (
        await rating_service.clear_episode_rating(session, user_id=user.id, episode_id=ep.id) == 0
    )


@pytest.mark.asyncio
async def test_friend_show_ratings_aggregate(session, make_user):
    me = await make_user(email="rsme@example.com")
    f1 = await make_user(email="rsf1@example.com", display_name="F1")
    f2 = await make_user(email="rsf2@example.com", display_name="F2")
    pending = await make_user(email="rsp@example.com", display_name="P")
    blocked = await make_user(email="rsb@example.com", display_name="B")
    stranger = await make_user(email="rss@example.com", display_name="S")
    await _accept_pair(session, me, f1)
    await _accept_pair(session, me, f2)
    # pending sent a request that's never accepted
    await connection_service.send_request(session, requester_id=pending.id, addressee_id=me.id)
    # me blocked `blocked`
    await connection_service.block(session, blocker_id=me.id, blocked_id=blocked.id)
    show = await _seed_show(session, show_id=8800201)
    await session.commit()

    # Seed ratings: f2 newest, then f1; plus pending, blocked, stranger (all excluded).
    now = datetime.now(UTC)
    await show_rating_repo.upsert(session, user_id=f1.id, show_id=show.id, stars=Decimal("4.0"))
    r_f1 = await show_rating_repo.get(session, user_id=f1.id, show_id=show.id)
    assert r_f1 is not None
    # Force f1 older than f2
    r_f1.rated_at = now - timedelta(minutes=10)
    await show_rating_repo.upsert(session, user_id=f2.id, show_id=show.id, stars=Decimal("5.0"))
    r_f2 = await show_rating_repo.get(session, user_id=f2.id, show_id=show.id)
    assert r_f2 is not None
    r_f2.rated_at = now
    one = Decimal("1.0")
    await show_rating_repo.upsert(session, user_id=pending.id, show_id=show.id, stars=one)
    await show_rating_repo.upsert(session, user_id=blocked.id, show_id=show.id, stars=one)
    await show_rating_repo.upsert(session, user_id=stranger.id, show_id=show.id, stars=one)
    await session.commit()

    out = await rating_service.friend_show_ratings(session, viewer_id=me.id, show_id=show.id)
    assert out.count == 2
    assert out.avg == pytest.approx(4.5)
    # newest first
    assert [i.user_id for i in out.items] == [f2.id, f1.id]


@pytest.mark.asyncio
async def test_friend_show_ratings_empty(session, make_user):
    me = await make_user(email="rsme2@example.com")
    show = await _seed_show(session, show_id=8800202)
    await session.commit()
    out = await rating_service.friend_show_ratings(session, viewer_id=me.id, show_id=show.id)
    assert out.count == 0
    assert out.avg is None
    assert out.items == []


@pytest.mark.asyncio
async def test_friend_show_ratings_404(session, make_user):
    me = await make_user(email="rsme3@example.com")
    await session.commit()
    with pytest.raises(NotFound):
        await rating_service.friend_show_ratings(session, viewer_id=me.id, show_id=9_999_999)


@pytest.mark.asyncio
async def test_friend_episode_ratings_aggregate_and_404(session, make_user):
    me = await make_user(email="reme@example.com")
    f1 = await make_user(email="ref1@example.com", display_name="EF1")
    await _accept_pair(session, me, f1)
    show = await _seed_show(session, show_id=8800301)
    ep = await _seed_episode(session, show_id=show.id, episode_id=88003011)
    await session.commit()

    await episode_rating_repo.upsert(session, user_id=f1.id, episode_id=ep.id, stars=Decimal("4.0"))
    await session.commit()

    out = await rating_service.friend_episode_ratings(session, viewer_id=me.id, episode_id=ep.id)
    assert out.count == 1
    assert out.avg == pytest.approx(4.0)
    assert out.items[0].user_id == f1.id

    with pytest.raises(NotFound):
        await rating_service.friend_episode_ratings(session, viewer_id=me.id, episode_id=9_999_999)
