"""The season list, which is the one payload NEU-1047 allows to differ.

`catalog.season` holds 18,339 copied rows NEU-1119 deliberately kept across
4,396 shows, so a show's season list is not what TMDB alone would give. The rule
is in `catalog/seasons.py`: **one row per `(show, season number)`, preferring the
row that carries a `tmdb_id`**. These tests pin the three shapes production
actually holds, plus the acceptance case the ticket names.

Ids are seeded explicitly and the ingested rows are given the *higher* id
throughout, so nothing here can pass by accident off insertion order — the copy
is always the older row, exactly as it is in production.
"""

import pytest

from tvbf.app.repos import season_repo
from tvbf.catalog import models as m
from tvbf.catalog import seasons as season_rules
from tvbf.catalog.browse_queries import get_show_seasons


async def _show(session, show_id: int, name: str, *, tmdb_id: int | None) -> m.Show:
    show = m.Show(id=show_id, tmdb_id=tmdb_id, name=name)
    session.add(show)
    await session.flush()
    return show


async def _season(
    session, *, season_id: int, show_id: int, number: int, tmdb_id: int | None, name: str = ""
) -> m.Season:
    season = m.Season(
        id=season_id,
        tmdb_id=tmdb_id,
        show_id=show_id,
        season_number=number,
        name=name or f"Season {number}",
    )
    session.add(season)
    await session.flush()
    return season


class TestOneRowPerSeasonNumber:
    async def test_the_ingested_row_wins_over_the_copy(self, session):
        """The 33 pairs still doubled at one number, and every pair the daily
        creates from here on. The copy carries TV Maze's data; the ingested row
        carries TMDB's, and only one of the two should reach a season picker."""
        await _show(session, 900, "Doubled", tmdb_id=900)
        await _season(session, season_id=1, show_id=900, number=1, tmdb_id=None, name="copied")
        await _season(session, season_id=2, show_id=900, number=1, tmdb_id=77, name="ingested")

        seasons = await get_show_seasons(session, 900)

        assert [(s.id, s.name) for s in seasons] == [(2, "ingested")]

    async def test_two_copies_at_one_number_resolve_to_the_older_row(self, session):
        """TV Maze's own duplicate numbering — 19 of the 33 are one show. Neither
        row carries a `tmdb_id`, so the tie breaks on the lowest id rather than
        on whatever order Postgres happens to return: `catalog.season`
        deliberately carries no `UNIQUE (show_id, season_number)`."""
        await _show(session, 901, "Wild Kingdom", tmdb_id=901)
        await _season(session, season_id=11, show_id=901, number=3, tmdb_id=None, name="first")
        await _season(session, season_id=12, show_id=901, number=3, tmdb_id=None, name="second")

        seasons = await get_show_seasons(session, 901)

        assert [(s.id, s.name) for s in seasons] == [(11, "first")]

    async def test_the_choice_is_stable_whichever_order_rows_arrive_in(self, session):
        """Pure-function check on the rule itself, both ways round, so the
        preference cannot silently become "last one wins"."""
        copy = m.Season(id=20, tmdb_id=None, show_id=1, season_number=1)
        ingested = m.Season(id=21, tmdb_id=5, show_id=1, season_number=1)

        assert season_rules.deduped([copy, ingested]) == [ingested]
        assert season_rules.deduped([ingested, copy]) == [ingested]


class TestWillAndGrace:
    """The acceptance case. TMDB models the revival as a **separate series**, so
    seasons 9–11 exist only as copied rows and two of them carry real watch
    history (17 records in production). A read path that preferred "the ingested
    set when a show has one" would hide episodes a user has already marked
    watched — the one thing this migration promised not to do.
    """

    @pytest.fixture
    async def will_and_grace(self, session):
        await _show(session, 549, "Will & Grace", tmdb_id=1_549)
        for number in (1, 2):
            await _season(
                session,
                season_id=100 + number,
                show_id=549,
                number=number,
                tmdb_id=500 + number,
            )
        for number in (9, 10, 11):
            await _season(session, season_id=100 + number, show_id=549, number=number, tmdb_id=None)
        await session.commit()

    async def test_the_revival_seasons_stay_in_the_season_list(self, session, will_and_grace):
        seasons = await get_show_seasons(session, 549)

        assert [s.season_number for s in seasons] == [1, 2, 9, 10, 11]

    async def test_a_watched_episode_on_a_revival_season_stays_reachable(
        self, session, will_and_grace, make_user, authed_client
    ):
        """End to end: the episode is on a locally-authored season, the user has
        watched it, and both the episode list and the watch record survive."""
        from tvbf.app.models import UserEpisodeWatch

        session.add(
            m.Episode(
                id=54_909_001,
                tmdb_id=None,
                show_id=549,
                season_id=109,
                season_number=9,
                episode_number=1,
                name="A New Lease on Life",
            )
        )
        await session.flush()
        session.add(UserEpisodeWatch(user_id=authed_client.user.id, episode_id=54_909_001))
        await session.commit()

        episodes = await authed_client.get("/shows/549/episodes?season=9")
        watched = await authed_client.get("/me/shows/549/episodes/watched")

        assert [e["id"] for e in episodes.json()] == [54_909_001]
        assert watched.json() == [54_909_001]


class TestTheYearVersusOrdinalSplit:
    async def test_both_numbering_schemes_survive(self, session):
        """10,193 seasons across 929 shows carry a calendar-year number beside
        TMDB's ordinal one. The two never collide, so no rule keyed on the
        number can tell them apart — and the only rule that could ("hide the
        copies") would hide episodes that exist nowhere else. The split is
        therefore *not* collapsed, and this test says so out loud rather than
        leaving the residue to be rediscovered as a bug.

        None of the 929 affected shows is tracked by a user, which is why the
        cost lands where it is cheapest.
        """
        await _show(session, 902, "Talking Movies", tmdb_id=902)
        for number in (1, 2):
            await _season(
                session, season_id=200 + number, show_id=902, number=number, tmdb_id=800 + number
            )
        for year in (2006, 2007):
            await _season(session, season_id=year, show_id=902, number=year, tmdb_id=None)

        seasons = await get_show_seasons(session, 902)

        assert [s.season_number for s in seasons] == [1, 2, 2006, 2007]


class TestUpcomingSeasonsAgrees:
    async def test_it_dedupes_before_filtering_to_unaired_not_after(self, session, make_user):
        """`/me/upcoming/seasons` has to apply the same rule, or a season that
        has already aired surfaces as upcoming on the strength of a copied row
        the show page does not display.

        Season 1 is doubled: the ingested row's episode aired, the copy has no
        episodes at all. Deduplicating after the unaired filter would leave only
        the copy in the candidate set and announce a season that is already out.
        """
        from datetime import date, timedelta

        from tvbf.app.models import UserShowWatch

        user = await make_user()
        await _show(session, 903, "Already Airing", tmdb_id=903)
        await _season(session, season_id=301, show_id=903, number=1, tmdb_id=None, name="copied")
        ingested = await _season(
            session, season_id=302, show_id=903, number=1, tmdb_id=90, name="ingested"
        )
        session.add(
            m.Episode(
                id=90_300_001,
                tmdb_id=9001,
                show_id=903,
                season_id=ingested.id,
                season_number=1,
                episode_number=1,
                air_date=date.today() - timedelta(days=7),
            )
        )
        session.add(UserShowWatch(user_id=user.id, show_id=903))
        await session.commit()

        unaired = await season_repo.unaired_for_shows(session, [903], date.today())

        assert unaired == []

    async def test_an_aired_season_stays_hidden_when_its_episodes_hang_off_the_copy(
        self, session, make_user
    ):
        """The mirror image, and the one a later delta produces: the episodes sit
        under the *losing* row. Asking the surviving row for its own episodes
        reports the season as episode-less, and an already-aired season is
        announced as upcoming — so the question is asked per season number.
        """
        from datetime import date, timedelta

        from tvbf.app.models import UserShowWatch

        user = await make_user()
        await _show(session, 904, "Delta Duplicate", tmdb_id=904)
        copy = await _season(
            session, season_id=401, show_id=904, number=1, tmdb_id=None, name="copied"
        )
        await _season(session, season_id=402, show_id=904, number=1, tmdb_id=91, name="ingested")
        session.add(
            m.Episode(
                id=90_400_001,
                tmdb_id=9101,
                show_id=904,
                season_id=copy.id,
                season_number=1,
                episode_number=1,
                air_date=date.today() - timedelta(days=7),
            )
        )
        session.add(UserShowWatch(user_id=user.id, show_id=904))
        await session.commit()

        unaired = await season_repo.unaired_for_shows(session, [904], date.today())

        assert unaired == []
