"""The never-recommend set, source by source (NEU-1175, NEU-1178).

Project spec §8 names four sources and this module is where that sentence lives,
so what is asserted here is that each one suppresses on its own, that they
compose, and that they are scoped to the user asking. NEU-1178's dismissal is the
fifth, and the one that is *not* a §8 record source: it can name a show the user
has never met, which is the property its own test below turns on.

Both shapes of the answer are exercised against the same rows, because a `Select`
used as an anti-join operand and a materialised `frozenset` disagreeing is
exactly the drift the module exists to prevent.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from tvbf.app.models import (
    UserEpisodeRating,
    UserEpisodeWatch,
    UserRecommendationDismissal,
    UserShowRating,
    UserShowWatch,
)
from tvbf.catalog.models import Episode, Show
from tvbf.recommendations import exclusion

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
AIRED = NOW.date() - timedelta(days=30)

MEMBERSHIP = 971_000
SHOW_RATED = 971_100
EPISODE_WATCHED = 971_200
EPISODE_RATED = 971_300
DISMISSED = 971_500
UNTOUCHED = 971_400

ALL_SHOWS = (MEMBERSHIP, SHOW_RATED, EPISODE_WATCHED, EPISODE_RATED, DISMISSED, UNTOUCHED)


@pytest.fixture
async def shows(session):
    """One show per source, plus one the user has never met."""
    session.add_all([Show(id=sid, name=f"Show {sid}", first_air_date=AIRED) for sid in ALL_SHOWS])
    await session.flush()
    session.add_all(
        [
            Episode(
                id=sid + n,
                show_id=sid,
                season_number=1,
                episode_number=n,
                air_date=AIRED,
            )
            for sid in ALL_SHOWS
            for n in (1, 2)
        ]
    )
    await session.commit()


async def _ids(session, user) -> frozenset[int]:
    """Both shapes of the answer, asserted equal, then returned.

    Every test goes through here so that neither shape can pass alone.
    """
    materialised = await exclusion.load_show_ids_never_to_recommend(session, user_id=user.id)
    via_select = frozenset(
        (
            await session.scalars(
                select(Show.id).where(Show.id.in_(exclusion.show_ids_never_to_recommend(user.id)))
            )
        ).all()
    )
    assert materialised == via_select
    return materialised


async def test_a_user_with_no_records_at_all_excludes_nothing(session, make_user, shows):
    user = await make_user()
    assert await _ids(session, user) == frozenset()


async def test_my_shows_membership_suppresses_on_its_own(session, make_user, shows):
    user = await make_user()
    session.add(UserShowWatch(user_id=user.id, show_id=MEMBERSHIP))
    await session.commit()

    assert await _ids(session, user) == {MEMBERSHIP}


async def test_a_show_rating_suppresses_on_its_own(session, make_user, shows):
    user = await make_user()
    session.add(UserShowRating(user_id=user.id, show_id=SHOW_RATED, stars=Decimal("4.5")))
    await session.commit()

    assert await _ids(session, user) == {SHOW_RATED}


async def test_an_episode_watch_suppresses_its_show_on_its_own(session, make_user, shows):
    user = await make_user()
    session.add(UserEpisodeWatch(user_id=user.id, episode_id=EPISODE_WATCHED + 1))
    await session.commit()

    assert await _ids(session, user) == {EPISODE_WATCHED}


async def test_an_episode_rating_suppresses_its_show_on_its_own(session, make_user, shows):
    """The source the narrow rule would miss: rating an episode never creates a
    My Shows row, because `show_membership_repo.add` has one caller."""
    user = await make_user()
    session.add(
        UserEpisodeRating(user_id=user.id, episode_id=EPISODE_RATED + 1, stars=Decimal("3.0"))
    )
    await session.commit()

    assert await _ids(session, user) == {EPISODE_RATED}


async def test_a_dismissal_suppresses_on_its_own(session, make_user, shows):
    """The fifth source, and the one no §8 record backs (NEU-1178).

    The user has never watched, rated or added this show — a dismissal is the
    whole reason it is excluded, which is what makes the endpoint's "the show
    need not be in the current set" rule coherent.
    """
    user = await make_user()
    session.add(UserRecommendationDismissal(user_id=user.id, show_id=DISMISSED))
    await session.commit()

    assert await _ids(session, user) == {DISMISSED}


async def test_all_five_sources_compose_and_the_untouched_show_is_absent(session, make_user, shows):
    user = await make_user()
    session.add_all(
        [
            UserShowWatch(user_id=user.id, show_id=MEMBERSHIP),
            UserShowRating(user_id=user.id, show_id=SHOW_RATED, stars=Decimal("5.0")),
            UserEpisodeWatch(user_id=user.id, episode_id=EPISODE_WATCHED + 1),
            UserEpisodeRating(user_id=user.id, episode_id=EPISODE_RATED + 1, stars=Decimal("2.0")),
            UserRecommendationDismissal(user_id=user.id, show_id=DISMISSED),
        ]
    )
    await session.commit()

    assert await _ids(session, user) == {
        MEMBERSHIP,
        SHOW_RATED,
        EPISODE_WATCHED,
        EPISODE_RATED,
        DISMISSED,
    }


async def test_one_show_reached_by_several_sources_is_named_once(session, make_user, shows):
    """`union_all` does not deduplicate; the answer is a set either way."""
    user = await make_user()
    session.add_all(
        [
            UserShowWatch(user_id=user.id, show_id=MEMBERSHIP),
            UserShowRating(user_id=user.id, show_id=MEMBERSHIP, stars=Decimal("4.0")),
            UserEpisodeWatch(user_id=user.id, episode_id=MEMBERSHIP + 1),
            UserEpisodeWatch(user_id=user.id, episode_id=MEMBERSHIP + 2),
        ]
    )
    await session.commit()

    assert await _ids(session, user) == {MEMBERSHIP}


async def test_another_users_records_never_appear(session, make_user, shows):
    mine = await make_user(email="mine@example.com")
    theirs = await make_user(email="theirs@example.com")
    session.add_all(
        [
            UserShowWatch(user_id=mine.id, show_id=MEMBERSHIP),
            UserShowWatch(user_id=theirs.id, show_id=SHOW_RATED),
            UserEpisodeWatch(user_id=theirs.id, episode_id=EPISODE_WATCHED + 1),
            UserEpisodeRating(
                user_id=theirs.id, episode_id=EPISODE_RATED + 1, stars=Decimal("1.0")
            ),
            UserRecommendationDismissal(user_id=theirs.id, show_id=DISMISSED),
        ]
    )
    await session.commit()

    assert await _ids(session, mine) == {MEMBERSHIP}
    assert await _ids(session, theirs) == {
        SHOW_RATED,
        EPISODE_WATCHED,
        EPISODE_RATED,
        DISMISSED,
    }


async def test_removing_the_record_removes_the_exclusion(session, make_user, shows):
    """It is a live query over the user's current state, never a stored flag."""
    user = await make_user()
    row = UserShowWatch(user_id=user.id, show_id=MEMBERSHIP)
    session.add(row)
    await session.commit()
    assert await _ids(session, user) == {MEMBERSHIP}

    await session.delete(row)
    await session.commit()

    assert await _ids(session, user) == frozenset()
