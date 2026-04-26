import datetime

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from tvbf.app.models import Session, User, UserEpisodeWatch, UserShowWatch
from tvbf.tvmaze.models import Episode, Show


@pytest.mark.asyncio
async def test_user_email_is_case_insensitive_unique(session):
    session.add(User(email="Alice@example.com", password_hash="x", display_name="Alice"))
    await session.commit()
    session.add(User(email="alice@example.com", password_hash="y", display_name="Alice2"))
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


@pytest.mark.asyncio
async def test_session_row_roundtrip(session):
    user = User(email="bob@example.com", password_hash="x", display_name="Bob")
    session.add(user)
    await session.flush()
    sess = Session(
        id="abc123",
        user_id=user.id,
        expires_at=datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=30),
    )
    session.add(sess)
    await session.commit()
    found = (await session.execute(select(Session).where(Session.id == "abc123"))).scalar_one()
    assert found.user_id == user.id


@pytest.mark.asyncio
async def test_user_show_watch_pk_prevents_duplicates(session):
    user = User(email="c@example.com", password_hash="x", display_name="C")
    session.add(user)
    show = Show(id=900001, name="X", tvmaze_updated=1)
    session.add(show)
    await session.flush()
    session.add(UserShowWatch(user_id=user.id, show_id=show.id))
    await session.commit()
    session.add(UserShowWatch(user_id=user.id, show_id=show.id))
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


@pytest.mark.asyncio
async def test_user_episode_watch_pk_prevents_duplicates(session):
    user = User(email="e@example.com", password_hash="x", display_name="E")
    session.add(user)
    show = Show(id=900003, name="X", tvmaze_updated=1)
    session.add(show)
    await session.flush()
    ep = Episode(id=900100, show_id=show.id, season=1, number=1)
    session.add(ep)
    await session.flush()
    session.add(UserEpisodeWatch(user_id=user.id, episode_id=ep.id))
    await session.commit()
    session.add(UserEpisodeWatch(user_id=user.id, episode_id=ep.id))
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


@pytest.mark.asyncio
async def test_user_delete_cascades_to_watch_tables(session):
    user = User(email="f@example.com", password_hash="x", display_name="F")
    session.add(user)
    show = Show(id=900004, name="X", tvmaze_updated=1)
    session.add(show)
    await session.flush()
    ep = Episode(id=900200, show_id=show.id, season=1, number=1)
    session.add(ep)
    await session.flush()
    session.add(UserShowWatch(user_id=user.id, show_id=show.id))
    session.add(UserEpisodeWatch(user_id=user.id, episode_id=ep.id))
    session.add(
        Session(
            id="sess_x",
            user_id=user.id,
            expires_at=datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=30),
        )
    )
    await session.commit()

    await session.execute(delete(User).where(User.id == user.id))
    await session.commit()

    show_watch = (
        await session.execute(select(UserShowWatch).where(UserShowWatch.user_id == user.id))
    ).all()
    ep_watch = (
        await session.execute(select(UserEpisodeWatch).where(UserEpisodeWatch.user_id == user.id))
    ).all()
    sess = (await session.execute(select(Session).where(Session.user_id == user.id))).all()
    assert show_watch == []
    assert ep_watch == []
    assert sess == []
