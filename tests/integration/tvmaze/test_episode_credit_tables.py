"""Shape of the episode-credit tables (NEU-959).

Table definitions only — no fetching, no routes. What is worth asserting is the
part real upstream data disagrees with: the credit keys have to be three-part,
because two-part ones are violated by episodes that legitimately credit one
person in two crew roles or one character to two people. See ADR-0003.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from tvbf.tvmaze import models as m


@pytest.fixture
async def catalog(session):
    """One show, one season, two episodes, two people, two crew roles."""
    session.add(m.Show(id=1, name="S", tvmaze_updated=1))
    await session.flush()
    session.add(m.Season(id=70, show_id=1, number=1))
    session.add_all(
        [
            m.Person(id=10, name="A Writer", tvmaze_updated=1),
            m.Person(id=11, name="A Director", tvmaze_updated=1),
        ]
    )
    session.add_all(
        [
            m.Character(id=900, name="Barista"),
            m.Character(id=901, name="Carl Carlson"),
        ]
    )
    session.add_all(
        [
            m.EpisodeCrewRole(id=1, name="Writer"),
            m.EpisodeCrewRole(id=2, name="Director"),
        ]
    )
    await session.flush()
    session.add_all(
        [
            m.Episode(id=500, show_id=1, season_id=70, season=1, number=1, name="E1"),
            m.Episode(id=501, show_id=1, season_id=70, season=1, number=2, name="E2"),
        ]
    )
    await session.commit()


async def test_episode_crew_role_names_are_interned(session, catalog):
    session.add(m.EpisodeCrewRole(id=3, name="Writer"))
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


async def test_one_person_may_hold_two_crew_roles_on_one_episode(session, catalog):
    """36 of 1,043 sampled episodes do this — a (episode_id, person_id) key
    would reject them."""
    session.add_all(
        [
            m.EpisodeCrew(episode_id=500, person_id=10, role_id=1, sort_order=0),
            m.EpisodeCrew(episode_id=500, person_id=10, role_id=2, sort_order=1),
        ]
    )
    await session.commit()

    rows = (
        (await session.execute(select(m.EpisodeCrew).where(m.EpisodeCrew.episode_id == 500)))
        .scalars()
        .all()
    )
    assert sorted(r.role_id for r in rows) == [1, 2]


async def test_episode_crew_rejects_a_true_duplicate(session, catalog):
    session.add(m.EpisodeCrew(episode_id=500, person_id=10, role_id=1, sort_order=0))
    await session.commit()

    session.add(m.EpisodeCrew(episode_id=500, person_id=10, role_id=1, sort_order=1))
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


async def test_episode_crew_requires_a_mirrored_episode(session, catalog):
    session.add(m.EpisodeCrew(episode_id=999999, person_id=10, role_id=1, sort_order=0))
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


async def test_deleting_an_episode_cascades_to_its_crew(session, catalog):
    session.add(m.EpisodeCrew(episode_id=500, person_id=10, role_id=1, sort_order=0))
    await session.commit()

    episode = (await session.execute(select(m.Episode).where(m.Episode.id == 500))).scalar_one()
    await session.delete(episode)
    await session.commit()

    assert (await session.execute(select(m.EpisodeCrew))).scalars().all() == []


async def test_one_character_may_be_played_by_two_people_on_one_episode(session, catalog):
    """17 of 1,043 sampled episodes do this — the person axis' own
    (episode_id, character_id) dedup key would silently drop one of them."""
    session.add_all(
        [
            m.EpisodeGuestCast(episode_id=500, person_id=10, character_id=901, sort_order=0),
            m.EpisodeGuestCast(episode_id=500, person_id=11, character_id=901, sort_order=1),
        ]
    )
    await session.commit()

    rows = (await session.execute(select(m.EpisodeGuestCast))).scalars().all()
    assert sorted(r.person_id for r in rows) == [10, 11]


async def test_one_person_may_play_two_characters_on_one_episode(session, catalog):
    session.add_all(
        [
            m.EpisodeGuestCast(episode_id=500, person_id=10, character_id=900, sort_order=0),
            m.EpisodeGuestCast(episode_id=500, person_id=10, character_id=901, sort_order=1),
        ]
    )
    await session.commit()

    rows = (await session.execute(select(m.EpisodeGuestCast))).scalars().all()
    assert sorted(r.character_id for r in rows) == [900, 901]


async def test_episode_guest_cast_rejects_a_true_duplicate(session, catalog):
    session.add(m.EpisodeGuestCast(episode_id=500, person_id=10, character_id=900, sort_order=0))
    await session.commit()

    session.add(m.EpisodeGuestCast(episode_id=500, person_id=10, character_id=900, sort_order=1))
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


async def test_season_credits_watermark_starts_null_and_is_settable(session, catalog):
    season = (await session.execute(select(m.Season).where(m.Season.id == 70))).scalar_one()
    assert season.credits_synced_at is None

    season.credits_synced_at = datetime.now(UTC)
    await session.commit()

    refreshed = (
        await session.execute(
            select(m.Season).where(m.Season.id == 70).execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert refreshed.credits_synced_at is not None
