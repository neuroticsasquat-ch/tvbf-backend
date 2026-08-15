"""The ledger: every episode-reading query and how it treats a special.

NEU-1062 rejected a shared `regular_episodes()` base selectable — no default is
right often enough, and a forgotten override fails **silently in the dangerous
direction** (unmark-show leaving orphan rows, an episode page 404ing a special).
Explicit-at-each-site fails loudly instead, but only if something notices the
site that forgot. This file is that something.

`LEDGER` names every public function in the five modules that read
`catalog.episode` on a user's behalf — `episode_repo`, `episode_watch_repo`,
`season_repo`, `activity_event_repo` and `episode_rating_repo` — and the
treatment each owes.
`test_every_query_has_a_ledger_row` fails the moment one is added without a row;
the behavioural tests below then hold each treatment to what it claims, against
a fixture show carrying all three shapes at once.

The last three modules are in scope because the acceptance criterion says
*every* episode-reading query, not every query in the two obvious repos — and
the activity-event pair is where the subtlest failure lived: a collapse wider
than the mark it substitutes for silently deletes a special's feed item.
`episode_rating_repo` joined the list when NEU-1103 gave it its first query
that reaches `catalog.episode` rather than being handed ids.
"""

import inspect
from datetime import date, datetime, timedelta
from decimal import Decimal

from tvbf.app.repos import (
    activity_event_repo,
    episode_rating_repo,
    episode_repo,
    episode_watch_repo,
    season_repo,
)
from tvbf.catalog.models import Episode, Show

# --- the ledger -------------------------------------------------------------

EXCLUDE_BOTH = "exclude both"
"""`~IS_SPECIAL` — show-level math, which specials must not move."""

EXCLUDE_COPIED = "exclude copied only"
"""`~IS_COPIED_SPECIAL` — per-season math, where season 0 reports itself."""

EXCLUDE_NOTHING = "exclude nothing"
"""Deliberately unfiltered; each one has a reason in its docstring."""

EXCLUDE_SPECIALS_SEASON = "exclude the specials season"
"""Season-grain: season 0 is dropped whole, because it is not a season a show
can be waiting for."""

BY_EXPLICIT_IDS = "not episode-scoped"
"""Writers and lookups the caller hands ids to — they filter nothing because
whoever built the id list already did."""

LEDGER: dict[str, str] = {
    # episode_repo
    "episode_repo.get_by_id": EXCLUDE_NOTHING,
    "episode_repo.list_episode_ids_for_season": EXCLUDE_COPIED,
    "episode_repo.list_all_episode_ids_for_season": EXCLUDE_NOTHING,
    "episode_repo.aired_count_per_season": EXCLUDE_COPIED,
    "episode_repo.list_aired_episode_ids_for_show": EXCLUDE_BOTH,
    "episode_repo.list_episode_ids_for_show": EXCLUDE_NOTHING,
    "episode_repo.count_per_show": EXCLUDE_BOTH,
    "episode_repo.count_aired_per_show": EXCLUDE_BOTH,
    "episode_repo.latest_aired_per_show": EXCLUDE_BOTH,
    "episode_repo.earliest_aired_unwatched_per_show": EXCLUDE_BOTH,
    "episode_repo.earliest_future_per_show": EXCLUDE_BOTH,
    "episode_repo.next_unwatched": EXCLUDE_BOTH,
    # episode_watch_repo
    "episode_watch_repo.watched_count_per_season": EXCLUDE_COPIED,
    "episode_watch_repo.count_watched_per_show": EXCLUDE_BOTH,
    "episode_watch_repo.list_episode_ids_for_show": EXCLUDE_NOTHING,
    "episode_watch_repo.list_show_ids_with_watches": EXCLUDE_NOTHING,
    "episode_watch_repo.user_ids_who_watched_show": EXCLUDE_NOTHING,
    "episode_watch_repo.first_watched_per_show": EXCLUDE_NOTHING,
    "episode_watch_repo.latest_watched_per_show": EXCLUDE_NOTHING,
    "episode_watch_repo.mark": BY_EXPLICIT_IDS,
    "episode_watch_repo.unmark": BY_EXPLICIT_IDS,
    "episode_watch_repo.bulk_mark": BY_EXPLICIT_IDS,
    "episode_watch_repo.bulk_unmark": BY_EXPLICIT_IDS,
    "episode_watch_repo.watched_in": BY_EXPLICIT_IDS,
    "episode_watch_repo.user_ids_who_watched_episode": BY_EXPLICIT_IDS,
    # season_repo
    "season_repo.unaired_for_shows": EXCLUDE_SPECIALS_SEASON,
    # activity_event_repo — the collapse must never be wider than the mark it
    # substitutes for, or a special's feed item vanishes while its watch stands.
    "activity_event_repo.delete_episode_events_for_season": EXCLUDE_COPIED,
    "activity_event_repo.delete_episode_and_season_events_for_show": EXCLUDE_BOTH,
    "activity_event_repo.upsert": BY_EXPLICIT_IDS,
    "activity_event_repo.delete": BY_EXPLICIT_IDS,
    # episode_rating_repo — a rating is a sentence somebody typed about an
    # episode, not progress, so the taste signal's mean counts specials.
    "episode_rating_repo.mean_stars_per_show_for_user": EXCLUDE_NOTHING,
    "episode_rating_repo.upsert": BY_EXPLICIT_IDS,
    "episode_rating_repo.delete": BY_EXPLICIT_IDS,
    "episode_rating_repo.get": BY_EXPLICIT_IDS,
    "episode_rating_repo.list_for_episode": BY_EXPLICIT_IDS,
    "episode_rating_repo.get_many_for_user": BY_EXPLICIT_IDS,
}


def _public_functions(module) -> set[str]:
    name = module.__name__.rsplit(".", 1)[-1]
    return {
        f"{name}.{fn}"
        for fn, obj in vars(module).items()
        if not fn.startswith("_")
        and inspect.iscoroutinefunction(obj)
        and obj.__module__ == module.__name__
    }


def test_every_query_has_a_ledger_row():
    """The tripwire. A query added to either repo with no row here is the
    signal that somebody made a specials decision without recording it."""
    actual = set().union(
        *(
            _public_functions(m)
            for m in (
                episode_repo,
                episode_watch_repo,
                season_repo,
                activity_event_repo,
                episode_rating_repo,
            )
        )
    )
    assert actual == set(LEDGER), {
        "missing from the ledger": sorted(actual - set(LEDGER)),
        "in the ledger but gone from the repos": sorted(set(LEDGER) - actual),
    }


# --- the fixture the behavioural tests run against ---------------------------

TODAY = date(2026, 8, 12)
AIRED = TODAY - timedelta(days=30)
FUTURE = TODAY + timedelta(days=30)

SHOW_ID = 962_000
#: season 1 holds 2 regular episodes and 1 copied special; season 0 holds 2
#: TMDB-native specials. Every count below is derived from this one shape.
REG_S1_E1, REG_S1_E2 = SHOW_ID + 1, SHOW_ID + 2
COPIED_S1 = SHOW_ID + 3
SPECIAL_S0_E1, SPECIAL_S0_E2 = SHOW_ID + 4, SHOW_ID + 5


async def _seed(session, *, show_id: int = SHOW_ID) -> Show:
    show = Show(id=show_id, name="Ledger", status="Ended")
    session.add(show)
    await session.flush()
    session.add_all(
        [
            Episode(
                id=REG_S1_E1, show_id=show_id, season_number=1, episode_number=1, air_date=AIRED
            ),
            Episode(
                id=REG_S1_E2, show_id=show_id, season_number=1, episode_number=2, air_date=AIRED
            ),
            Episode(
                id=COPIED_S1, show_id=show_id, season_number=1, episode_number=-1, air_date=AIRED
            ),
            Episode(
                id=SPECIAL_S0_E1, show_id=show_id, season_number=0, episode_number=1, air_date=AIRED
            ),
            Episode(
                id=SPECIAL_S0_E2, show_id=show_id, season_number=0, episode_number=2, air_date=AIRED
            ),
        ]
    )
    await session.flush()
    return show


async def _watch_everything(session, user_id) -> None:
    await episode_watch_repo.bulk_mark(
        session,
        user_id=user_id,
        episode_ids=[REG_S1_E1, REG_S1_E2, COPIED_S1, SPECIAL_S0_E1, SPECIAL_S0_E2],
        watched_at=datetime(2026, 8, 1, tzinfo=None).astimezone(),
    )
    await session.flush()


# --- exclude both ------------------------------------------------------------


class TestExcludeBoth:
    """Show-level math: 2 regular episodes, whatever else the show carries."""

    async def test_count_per_show(self, session):
        await _seed(session)
        assert await episode_repo.count_per_show(session, [SHOW_ID]) == {SHOW_ID: 2}

    async def test_count_aired_per_show(self, session):
        await _seed(session)
        assert await episode_repo.count_aired_per_show(session, [SHOW_ID], TODAY) == {SHOW_ID: 2}

    async def test_latest_aired_per_show_ignores_a_later_special(self, session):
        show = await _seed(session)
        later = TODAY - timedelta(days=1)
        session.add(
            Episode(
                id=SHOW_ID + 9,
                show_id=show.id,
                season_number=0,
                episode_number=3,
                air_date=later,
            )
        )
        await session.flush()

        assert await episode_repo.latest_aired_per_show(session, [SHOW_ID], TODAY) == {
            SHOW_ID: AIRED
        }

    async def test_list_aired_episode_ids_for_show(self, session):
        await _seed(session)
        ids = await episode_repo.list_aired_episode_ids_for_show(session, SHOW_ID, TODAY)
        assert sorted(ids) == [REG_S1_E1, REG_S1_E2]

    async def test_count_watched_per_show(self, session, make_user):
        user = await make_user()
        await _seed(session)
        await _watch_everything(session, user.id)

        counts = await episode_watch_repo.count_watched_per_show(
            session, user_id=user.id, show_ids=[SHOW_ID]
        )
        assert counts == {SHOW_ID: 2}

    async def test_next_unwatched_skips_specials(self, session, make_user):
        user = await make_user()
        await _seed(session)
        await episode_watch_repo.bulk_mark(
            session,
            user_id=user.id,
            episode_ids=[REG_S1_E1, REG_S1_E2],
            watched_at=datetime.now().astimezone(),
        )
        await session.flush()

        # Both specials are unwatched, and neither is offered.
        assert await episode_repo.next_unwatched(session, user_id=user.id, show_id=SHOW_ID) is None

    async def test_earliest_aired_unwatched_per_show_skips_specials(self, session, make_user):
        from tvbf.app.models import UserShowWatch

        user = await make_user()
        await _seed(session)
        session.add(UserShowWatch(user_id=user.id, show_id=SHOW_ID))
        await session.flush()
        await episode_watch_repo.bulk_mark(
            session,
            user_id=user.id,
            episode_ids=[REG_S1_E1, REG_S1_E2],
            watched_at=datetime.now().astimezone(),
        )
        await session.flush()

        assert (
            await episode_repo.earliest_aired_unwatched_per_show(
                session, user_id=user.id, today=TODAY
            )
            == []
        )

    async def test_earliest_future_per_show_skips_an_upcoming_special(self, session, make_user):
        from tvbf.app.models import UserShowWatch

        user = await make_user()
        show = await _seed(session)
        session.add(UserShowWatch(user_id=user.id, show_id=SHOW_ID))
        # A special airs before the next regular episode; Upcoming must report
        # the regular one, not the special.
        session.add_all(
            [
                Episode(
                    id=SHOW_ID + 20,
                    show_id=show.id,
                    season_number=0,
                    episode_number=3,
                    air_date=FUTURE,
                ),
                Episode(
                    id=SHOW_ID + 21,
                    show_id=show.id,
                    season_number=2,
                    episode_number=1,
                    air_date=FUTURE + timedelta(days=7),
                ),
            ]
        )
        await session.flush()

        episodes = await episode_repo.earliest_future_per_show(
            session, user_id=user.id, today=TODAY
        )
        assert [e.id for e in episodes] == [SHOW_ID + 21]


# --- exclude copied only -----------------------------------------------------


class TestExcludeCopiedOnly:
    """Per-season math: season 0 reports its own contents, and a real season
    does not count the copied special hanging inside it."""

    async def test_aired_count_per_season(self, session):
        await _seed(session)
        assert await episode_repo.aired_count_per_season(session, SHOW_ID, TODAY) == {0: 2, 1: 2}

    async def test_watched_count_per_season(self, session, make_user):
        user = await make_user()
        await _seed(session)
        await _watch_everything(session, user.id)

        counts = await episode_watch_repo.watched_count_per_season(
            session, user_id=user.id, show_id=SHOW_ID
        )
        assert counts == {0: 2, 1: 2}

    async def test_list_episode_ids_for_season_keeps_season_zero_whole(self, session):
        await _seed(session)
        assert sorted(await episode_repo.list_episode_ids_for_season(session, SHOW_ID, 0)) == [
            SPECIAL_S0_E1,
            SPECIAL_S0_E2,
        ]

    async def test_list_episode_ids_for_season_drops_the_copied_special(self, session):
        await _seed(session)
        assert sorted(await episode_repo.list_episode_ids_for_season(session, SHOW_ID, 1)) == [
            REG_S1_E1,
            REG_S1_E2,
        ]


# --- exclude nothing ---------------------------------------------------------


class TestExcludeNothing:
    """The three sites that must keep seeing specials, and why."""

    async def test_get_by_id_serves_a_special(self, session):
        await _seed(session)
        # A special's own episode page: filtering here would 404 it.
        assert (await episode_repo.get_by_id(session, COPIED_S1)) is not None
        assert (await episode_repo.get_by_id(session, SPECIAL_S0_E1)) is not None

    async def test_list_all_episode_ids_for_season_backs_unmark_and_keeps_everything(self, session):
        await _seed(session)
        ids = await episode_repo.list_all_episode_ids_for_season(session, SHOW_ID, 1)
        assert sorted(ids) == sorted([REG_S1_E1, REG_S1_E2, COPIED_S1])

    async def test_list_episode_ids_for_show_backs_unmark_and_keeps_everything(self, session):
        await _seed(session)
        ids = await episode_repo.list_episode_ids_for_show(session, SHOW_ID)
        assert sorted(ids) == sorted(
            [REG_S1_E1, REG_S1_E2, COPIED_S1, SPECIAL_S0_E1, SPECIAL_S0_E2]
        )

    async def test_watched_ids_still_report_a_watched_special(self, session, make_user):
        user = await make_user()
        await _seed(session)
        await _watch_everything(session, user.id)

        # `GET /me/shows/{id}/episodes/watched` — the ticks on the show page.
        ids = await episode_watch_repo.list_episode_ids_for_show(
            session, user_id=user.id, show_id=SHOW_ID
        )
        assert sorted(ids) == sorted(
            [REG_S1_E1, REG_S1_E2, COPIED_S1, SPECIAL_S0_E1, SPECIAL_S0_E2]
        )

    async def test_episode_rating_mean_counts_a_rated_special(self, session, make_user):
        user = await make_user()
        await _seed(session)
        # 4 stars on a regular episode, 2 on a season-0 special: the mean the
        # taste signal reads is 3, not the 4 a specials filter would leave.
        await episode_rating_repo.upsert(
            session, user_id=user.id, episode_id=REG_S1_E1, stars=Decimal("4.0")
        )
        await episode_rating_repo.upsert(
            session, user_id=user.id, episode_id=SPECIAL_S0_E1, stars=Decimal("2.0")
        )
        await session.flush()

        means = await episode_rating_repo.mean_stars_per_show_for_user(session, user_id=user.id)
        assert means == {SHOW_ID: 3.0}
