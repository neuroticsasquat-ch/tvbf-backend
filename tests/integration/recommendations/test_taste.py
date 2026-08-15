"""`taste_for_user` against seeded rows: which shows enter, and which rating wins.

The tier arithmetic is pinned by the unit tests. What only a database can answer
is the universe — My Shows, watches and show ratings, and nothing else — and the
precedence between a show rating and the mean of a user's episode ratings.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tvbf.app.models import UserShowWatch
from tvbf.app.repos import episode_rating_repo, episode_watch_repo, show_rating_repo
from tvbf.catalog.models import Episode, Show
from tvbf.recommendations.taste import TasteLabel, taste_for_user

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
AIRED = NOW.date() - timedelta(days=30)
RECENTLY = NOW - timedelta(days=3)
LONG_AGO = NOW - timedelta(days=400)

TRACKED = 964_000
WATCHED_ONLY = 964_100
RATED_ONLY = 964_200
UNTOUCHED = 964_300
EPISODE_RATED = 964_400


def _episodes(show_id: int, count: int) -> list[Episode]:
    return [
        Episode(
            id=show_id + n,
            show_id=show_id,
            season_number=1,
            episode_number=n,
            air_date=AIRED,
        )
        for n in range(1, count + 1)
    ]


@pytest.fixture
async def shows(session):
    session.add_all(
        [
            Show(id=TRACKED, name="Tracked", status="Ended"),
            Show(id=WATCHED_ONLY, name="Watched Only", status="Ended"),
            Show(id=RATED_ONLY, name="Rated Only", status="Ended"),
            Show(id=UNTOUCHED, name="Untouched", status="Ended"),
            Show(id=EPISODE_RATED, name="Episode Rated", status="Ended"),
        ]
    )
    await session.flush()
    session.add_all(
        [
            *_episodes(TRACKED, 4),
            *_episodes(WATCHED_ONLY, 4),
            *_episodes(RATED_ONLY, 4),
            *_episodes(UNTOUCHED, 4),
            *_episodes(EPISODE_RATED, 4),
        ]
    )
    await session.flush()


async def _track(session, user_id, show_id: int) -> None:
    session.add(UserShowWatch(user_id=user_id, show_id=show_id))
    await session.flush()


async def _watch(session, user_id, episode_ids, *, when: datetime) -> None:
    await episode_watch_repo.bulk_mark(
        session, user_id=user_id, episode_ids=episode_ids, watched_at=when
    )
    await session.flush()


class TestTheUniverse:
    async def test_my_shows_watches_and_show_ratings_all_enrol_a_show(
        self, session, make_user, shows
    ):
        user = await make_user()
        await _track(session, user.id, TRACKED)
        await _watch(session, user.id, [WATCHED_ONLY + 1], when=RECENTLY)
        await show_rating_repo.upsert(
            session, user_id=user.id, show_id=RATED_ONLY, stars=Decimal("4.5")
        )
        await session.flush()

        signals = await taste_for_user(session, user_id=user.id, now=NOW)

        assert set(signals) == {TRACKED, WATCHED_ONLY, RATED_ONLY}
        assert signals[TRACKED].label is TasteLabel.INTERESTED
        assert signals[RATED_ONLY].label is TasteLabel.LIKED

    async def test_a_rated_show_survives_being_removed_from_my_shows(
        self, session, make_user, shows
    ):
        """A rating outranks membership, so it must outlive it too."""
        user = await make_user()
        await show_rating_repo.upsert(
            session, user_id=user.id, show_id=RATED_ONLY, stars=Decimal("5.0")
        )
        await session.flush()

        signals = await taste_for_user(session, user_id=user.id, now=NOW)

        assert signals[RATED_ONLY].label is TasteLabel.LIKED
        assert signals[RATED_ONLY].in_my_shows is False
        assert signals[RATED_ONLY].added_at is None

    async def test_an_episode_rating_alone_does_not_enrol_a_show(self, session, make_user, shows):
        """Spec §13: episode ratings refine a show already in the universe; they
        are not a signal of their own."""
        user = await make_user()
        await episode_rating_repo.upsert(
            session, user_id=user.id, episode_id=EPISODE_RATED + 1, stars=Decimal("5.0")
        )
        await session.flush()

        assert await taste_for_user(session, user_id=user.id, now=NOW) == {}

    async def test_another_users_history_never_enters(self, session, make_user, shows):
        user = await make_user()
        other = await make_user(email="other@example.com")
        await _track(session, other.id, TRACKED)
        await show_rating_repo.upsert(
            session, user_id=other.id, show_id=RATED_ONLY, stars=Decimal("5.0")
        )
        await _watch(session, other.id, [WATCHED_ONLY + 1], when=RECENTLY)
        await session.flush()

        assert await taste_for_user(session, user_id=user.id, now=NOW) == {}

    async def test_a_user_with_no_history_at_all(self, session, make_user):
        user = await make_user()
        assert await taste_for_user(session, user_id=user.id, now=NOW) == {}


class TestWhichRating:
    async def test_a_show_rating_wins_outright(self, session, make_user, shows):
        """Two 1-star episode ratings do not drag a 4.5-star show down."""
        user = await make_user()
        await _track(session, user.id, TRACKED)
        await show_rating_repo.upsert(
            session, user_id=user.id, show_id=TRACKED, stars=Decimal("4.5")
        )
        for episode_id in (TRACKED + 1, TRACKED + 2):
            await episode_rating_repo.upsert(
                session, user_id=user.id, episode_id=episode_id, stars=Decimal("1.0")
            )
        await session.flush()

        signals = await taste_for_user(session, user_id=user.id, now=NOW)

        assert signals[TRACKED].stars == 4.5
        assert signals[TRACKED].label is TasteLabel.LIKED

    async def test_episode_ratings_are_averaged_when_no_show_rating_exists(
        self, session, make_user, shows
    ):
        user = await make_user()
        await _track(session, user.id, TRACKED)
        await episode_rating_repo.upsert(
            session, user_id=user.id, episode_id=TRACKED + 1, stars=Decimal("1.0")
        )
        await episode_rating_repo.upsert(
            session, user_id=user.id, episode_id=TRACKED + 2, stars=Decimal("2.0")
        )
        await session.flush()

        signals = await taste_for_user(session, user_id=user.id, now=NOW)

        assert signals[TRACKED].stars == 1.5
        assert signals[TRACKED].label is TasteLabel.NOT_LIKED

    async def test_no_rating_at_all_leaves_stars_null(self, session, make_user, shows):
        user = await make_user()
        await _track(session, user.id, TRACKED)

        signals = await taste_for_user(session, user_id=user.id, now=NOW)

        assert signals[TRACKED].stars is None


class TestTheFactsTravelWithTheLabel:
    async def test_completion_membership_and_added_at_are_reported(self, session, make_user, shows):
        """NEU-1104 reads all four; recomputing them there would be a second
        chance to disagree with this module."""
        user = await make_user()
        await _track(session, user.id, TRACKED)
        await _watch(session, user.id, [TRACKED + 1, TRACKED + 2], when=RECENTLY)

        signal = (await taste_for_user(session, user_id=user.id, now=NOW))[TRACKED]

        assert signal.completion.watched_episodes == 2
        assert signal.completion.aired_episodes == 4
        assert signal.completion.pct == 50
        assert signal.in_my_shows is True
        assert signal.added_at is not None
        assert signal.label is TasteLabel.LIKED

    async def test_an_uncovered_show_is_reported_with_no_label(self, session, make_user, shows):
        """Started three days ago and never added: too recent for NOT LIKED, too
        little for LIKED. The caller filters it, this module does not hide it."""
        user = await make_user()
        await _watch(session, user.id, [WATCHED_ONLY + 1], when=RECENTLY)

        signals = await taste_for_user(session, user_id=user.id, now=NOW)

        assert signals[WATCHED_ONLY].label is None
        assert signals[WATCHED_ONLY].completion.watched_episodes == 1

    async def test_the_abandonment_clause_reads_the_stored_watch_time(
        self, session, make_user, shows
    ):
        """The same show, watched in 2025 instead of last week."""
        user = await make_user()
        await _watch(session, user.id, [WATCHED_ONLY + 1], when=LONG_AGO)

        signals = await taste_for_user(session, user_id=user.id, now=NOW)

        assert signals[WATCHED_ONLY].label is TasteLabel.NOT_LIKED
