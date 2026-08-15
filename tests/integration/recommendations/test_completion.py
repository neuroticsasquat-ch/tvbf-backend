"""`completion_for_shows` against seeded `catalog` rows.

The fixture show carries every shape at once — regular aired episodes, an
unaired one, a copied special (negative number inside a real season) and a
TMDB-native season-0 special — so each assertion below is about which of them the
query counted, not about arithmetic the unit tests already pin.
"""

from datetime import date, datetime, timedelta

import pytest

from tvbf.app.repos import episode_watch_repo
from tvbf.catalog.models import Episode, Show
from tvbf.recommendations.completion import completion_for_shows

TODAY = date(2026, 8, 15)
AIRED = TODAY - timedelta(days=30)
FUTURE = TODAY + timedelta(days=30)

SHOW_ID = 963_000
OTHER_SHOW_ID = 963_100
UNDATED_SHOW_ID = 963_200

REG_E1, REG_E2 = SHOW_ID + 1, SHOW_ID + 2
UNAIRED = SHOW_ID + 3
COPIED_SPECIAL = SHOW_ID + 4
SEASON_0_SPECIAL = SHOW_ID + 5


async def _seed(session) -> None:
    session.add_all(
        [
            Show(id=SHOW_ID, name="Airing", status="Returning Series"),
            Show(id=OTHER_SHOW_ID, name="Untouched", status="Ended"),
        ]
    )
    await session.flush()
    session.add_all(
        [
            Episode(id=REG_E1, show_id=SHOW_ID, season_number=1, episode_number=1, air_date=AIRED),
            Episode(id=REG_E2, show_id=SHOW_ID, season_number=1, episode_number=2, air_date=AIRED),
            Episode(
                id=UNAIRED, show_id=SHOW_ID, season_number=1, episode_number=3, air_date=FUTURE
            ),
            Episode(
                id=COPIED_SPECIAL,
                show_id=SHOW_ID,
                season_number=1,
                episode_number=-1,
                air_date=AIRED,
            ),
            Episode(
                id=SEASON_0_SPECIAL,
                show_id=SHOW_ID,
                season_number=0,
                episode_number=1,
                air_date=AIRED,
            ),
            Episode(
                id=OTHER_SHOW_ID + 1,
                show_id=OTHER_SHOW_ID,
                season_number=1,
                episode_number=1,
                air_date=AIRED,
            ),
        ]
    )
    await session.flush()


async def _watch(session, user_id, episode_ids, *, when: datetime) -> None:
    await episode_watch_repo.bulk_mark(
        session, user_id=user_id, episode_ids=episode_ids, watched_at=when
    )
    await session.flush()


@pytest.fixture
def when():
    return datetime(2026, 8, 1, 12, 0).astimezone()


class TestTheDenominator:
    async def test_caught_up_on_an_airing_show_reads_a_hundred(self, session, make_user, when):
        """The ticket: the unaired episode is not held against the user."""
        user = await make_user()
        await _seed(session)
        await _watch(session, user.id, [REG_E1, REG_E2], when=when)

        rows = await completion_for_shows(session, user_id=user.id, show_ids=[SHOW_ID], today=TODAY)

        assert rows[SHOW_ID].aired_episodes == 2
        assert rows[SHOW_ID].watched_episodes == 2
        assert rows[SHOW_ID].pct == 100

    async def test_specials_count_in_neither_half(self, session, make_user, when):
        """Watching both specials and neither regular episode is still 0%."""
        user = await make_user()
        await _seed(session)
        await _watch(session, user.id, [COPIED_SPECIAL, SEASON_0_SPECIAL], when=when)

        rows = await completion_for_shows(session, user_id=user.id, show_ids=[SHOW_ID], today=TODAY)

        assert rows[SHOW_ID].watched_episodes == 0
        assert rows[SHOW_ID].aired_episodes == 2
        assert rows[SHOW_ID].pct == 0

    async def test_an_undated_episode_is_countable_by_nobody(self, session, make_user, when):
        """The only show shape with a zero denominator: watch it and it reads
        100, because there is nothing left that has aired."""
        user = await make_user()
        session.add(Show(id=UNDATED_SHOW_ID, name="Undated", status="Planned"))
        await session.flush()
        session.add(
            Episode(
                id=UNDATED_SHOW_ID + 1,
                show_id=UNDATED_SHOW_ID,
                season_number=1,
                episode_number=1,
                air_date=None,
            )
        )
        await session.flush()
        await _watch(session, user.id, [UNDATED_SHOW_ID + 1], when=when)

        rows = await completion_for_shows(
            session, user_id=user.id, show_ids=[UNDATED_SHOW_ID], today=TODAY
        )

        assert rows[UNDATED_SHOW_ID].aired_episodes == 0
        assert rows[UNDATED_SHOW_ID].watched_episodes == 1
        assert rows[UNDATED_SHOW_ID].pct == 100

    async def test_an_episode_airing_today_counts(self, session, make_user):
        user = await make_user()
        await _seed(session)

        rows = await completion_for_shows(
            session, user_id=user.id, show_ids=[SHOW_ID], today=FUTURE
        )

        assert rows[SHOW_ID].aired_episodes == 3


class TestRecency:
    async def test_last_watched_at_is_the_most_recent_watch(self, session, make_user):
        user = await make_user()
        await _seed(session)
        old = datetime(2019, 3, 1, 12, 0).astimezone()
        recent = datetime(2026, 8, 1, 12, 0).astimezone()
        await _watch(session, user.id, [REG_E1], when=old)
        await _watch(session, user.id, [REG_E2], when=recent)

        rows = await completion_for_shows(session, user_id=user.id, show_ids=[SHOW_ID], today=TODAY)

        assert rows[SHOW_ID].last_watched_at == recent

    async def test_a_watched_special_still_counts_as_engagement(self, session, make_user):
        """The 180-day abandonment clause asks when somebody last engaged, and
        watching a special is engaging — even though it moves no percentage."""
        user = await make_user()
        await _seed(session)
        old = datetime(2019, 3, 1, 12, 0).astimezone()
        recent = datetime(2026, 8, 1, 12, 0).astimezone()
        await _watch(session, user.id, [REG_E1], when=old)
        await _watch(session, user.id, [SEASON_0_SPECIAL], when=recent)

        rows = await completion_for_shows(session, user_id=user.id, show_ids=[SHOW_ID], today=TODAY)

        assert rows[SHOW_ID].pct == 50
        assert rows[SHOW_ID].last_watched_at == recent


class TestCoverage:
    async def test_every_requested_show_gets_a_row(self, session, make_user, when):
        """A My Shows row with no watches is the INTERESTED tier, so it must not
        come back as a missing key."""
        user = await make_user()
        await _seed(session)
        await _watch(session, user.id, [REG_E1], when=when)

        rows = await completion_for_shows(
            session, user_id=user.id, show_ids=[SHOW_ID, OTHER_SHOW_ID], today=TODAY
        )

        assert set(rows) == {SHOW_ID, OTHER_SHOW_ID}
        assert rows[OTHER_SHOW_ID].watched_episodes == 0
        assert rows[OTHER_SHOW_ID].aired_episodes == 1
        assert rows[OTHER_SHOW_ID].last_watched_at is None

    async def test_no_shows_asks_nothing(self, session, make_user):
        user = await make_user()
        assert await completion_for_shows(session, user_id=user.id, show_ids=[]) == {}

    async def test_another_users_watches_do_not_leak(self, session, make_user, when):
        watcher = await make_user(email="watcher@example.com")
        other = await make_user(email="other@example.com")
        await _seed(session)
        await _watch(session, watcher.id, [REG_E1, REG_E2], when=when)

        rows = await completion_for_shows(
            session, user_id=other.id, show_ids=[SHOW_ID], today=TODAY
        )

        assert rows[SHOW_ID].watched_episodes == 0
        assert rows[SHOW_ID].last_watched_at is None
        assert rows[SHOW_ID].pct == 0
