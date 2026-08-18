"""`build_payload` against seeded rows: order, tiers, the cap, and exclusion.

The floor arithmetic and the canonical form are pinned by the unit tests. What
only a database can answer is the row order — the fold it sorts on is evaluated
in Postgres and is not reproducible in Python (`sql_fold`) — the INTERESTED cap,
and the exclusion set being wider than the rows.
"""

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from tvbf.app.models import UserRecommendationDismissal, UserShowWatch
from tvbf.app.repos import episode_rating_repo, episode_watch_repo, show_rating_repo
from tvbf.catalog.models import Episode, Show
from tvbf.recommendations.payload import (
    INTERESTED_CAP,
    PROMPT_VERSION,
    build_payload,
    payload_hash,
)
from tvbf.recommendations.taste import taste_for_user

MODEL = "deepseek-ai/DeepSeek-V4-Pro-0813"
NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
AIRED = NOW.date() - timedelta(days=30)
RECENTLY = NOW - timedelta(days=3)

FIRST = 965_000
SECOND = 965_100
THIRD = 965_200
UNDATED = 965_300
EPISODE_RATED = 965_400
REBOOT_OLD = 965_500
REBOOT_NEW = 965_600


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
            # Chosen so the folded order and the raw order disagree at every
            # position: raw is Apples / Z-List / zebra / Émigré (hyphen before
            # letters, upper before lower, É past both), folded is apples /
            # emigre / zebra / zlist. A test whose fixture sorts the same either
            # way pins nothing.
            Show(id=FIRST, name="Z-List", first_air_date=AIRED, status="Ended"),
            Show(id=SECOND, name="Émigré", first_air_date=AIRED, status="Ended"),
            Show(id=THIRD, name="Apples", first_air_date=AIRED, status="Ended"),
            Show(id=UNDATED, name="zebra", status="Ended"),
            Show(id=EPISODE_RATED, name="Episode Rated", first_air_date=AIRED, status="Ended"),
            # Both fold to "reboot", so only the year separates them.
            Show(id=REBOOT_OLD, name="Re-Boot", first_air_date=date(2001, 3, 1), status="Ended"),
            Show(id=REBOOT_NEW, name="ReBoot", first_air_date=date(2019, 3, 1), status="Ended"),
        ]
    )
    await session.flush()
    session.add_all(
        [
            *_episodes(FIRST, 4),
            *_episodes(SECOND, 4),
            *_episodes(THIRD, 4),
            *_episodes(UNDATED, 4),
            *_episodes(EPISODE_RATED, 4),
            *_episodes(REBOOT_OLD, 4),
            *_episodes(REBOOT_NEW, 4),
        ]
    )
    await session.flush()


async def _track(session, user_id, show_id: int, *, added_at: datetime | None = None) -> None:
    row = UserShowWatch(user_id=user_id, show_id=show_id)
    if added_at is not None:
        row.created_at = added_at
    session.add(row)
    await session.flush()


def _document(payload) -> dict:
    return json.loads(payload.json)


class TestTheShape:
    async def test_a_row_is_title_year_pct_and_stars(self, session, make_user, shows):
        user = await make_user()
        await _track(session, user.id, THIRD)
        await episode_watch_repo.bulk_mark(
            session, user_id=user.id, episode_ids=[THIRD + 1, THIRD + 2], watched_at=RECENTLY
        )
        await show_rating_repo.upsert(session, user_id=user.id, show_id=THIRD, stars=Decimal("4.5"))
        await session.flush()

        document = _document(await build_payload(session, user_id=user.id, model=MODEL, now=NOW))

        assert document["columns"] == ["title", "year", "pct", "stars"]
        assert document["liked"] == [["Apples", AIRED.year, 50, 4.5]]

    async def test_a_show_with_no_premiere_date_reports_a_null_year(
        self, session, make_user, shows
    ):
        user = await make_user()
        await _track(session, user.id, UNDATED)

        document = _document(await build_payload(session, user_id=user.id, model=MODEL, now=NOW))

        assert document["interested"] == [["zebra", None, 0, None]]

    async def test_a_synthesized_rating_is_reported_to_one_decimal(self, session, make_user, shows):
        """`taste.py` classifies on the raw mean; the payload rounds, because a
        dozen digits claim a precision half-stars do not have."""
        user = await make_user()
        await _track(session, user.id, THIRD)
        for episode_id, stars in ((THIRD + 1, "4.0"), (THIRD + 2, "4.5"), (THIRD + 3, "5.0")):
            await episode_rating_repo.upsert(
                session, user_id=user.id, episode_id=episode_id, stars=Decimal(stars)
            )
        await session.flush()

        document = _document(await build_payload(session, user_id=user.id, model=MODEL, now=NOW))

        assert document["liked"][0][3] == 4.5

    async def test_every_tier_is_present_even_when_empty(self, session, make_user):
        user = await make_user()

        document = _document(await build_payload(session, user_id=user.id, model=MODEL, now=NOW))

        assert document["liked"] == []
        assert document["not_liked"] == []
        assert document["interested"] == []


class TestRowOrder:
    async def test_rows_sort_by_folded_title(self, session, make_user, shows):
        """Insertion order, id order and raw-name order all disagree with this."""
        user = await make_user()
        for show_id in (FIRST, SECOND, THIRD, UNDATED):
            await _track(session, user.id, show_id)

        document = _document(await build_payload(session, user_id=user.id, model=MODEL, now=NOW))

        assert [row[0] for row in document["interested"]] == [
            "Apples",
            "Émigré",
            "zebra",
            "Z-List",
        ]

    async def test_a_folded_tie_breaks_on_the_year(self, session, make_user, shows):
        """ "Re-Boot" and "ReBoot" fold to the same string, so without the second
        term the planner picks the order and every hash churns."""
        user = await make_user()
        for show_id in (REBOOT_NEW, REBOOT_OLD):
            await _track(session, user.id, show_id)

        document = _document(await build_payload(session, user_id=user.id, model=MODEL, now=NOW))

        assert [row[1] for row in document["interested"]] == [2001, 2019]

    async def test_the_same_history_hashes_the_same_twice(self, session, make_user, shows):
        user = await make_user()
        for show_id in (FIRST, SECOND, THIRD):
            await _track(session, user.id, show_id)

        first = await build_payload(session, user_id=user.id, model=MODEL, now=NOW)
        second = await build_payload(session, user_id=user.id, model=MODEL, now=NOW)

        assert first.hash == second.hash

    async def test_a_different_model_or_prompt_version_moves_the_hash(
        self, session, make_user, shows
    ):
        """Shipping a prompt change re-runs everyone exactly once (§9.1).

        Both versions are read off `PROMPT_VERSION` rather than written out: this
        test pinned the literal `"1"` and used `"2"` as its counter-example, so
        the first bump failed it on the very behaviour it exists to assert.
        """
        user = await make_user()
        await _track(session, user.id, THIRD)

        payload = await build_payload(session, user_id=user.id, model=MODEL, now=NOW)

        assert payload.hash == payload_hash(
            prompt_version=PROMPT_VERSION, model=MODEL, canonical_json=payload.json
        )
        assert (
            await build_payload(session, user_id=user.id, model="other", now=NOW)
        ).hash != payload.hash
        assert (
            await build_payload(
                session,
                user_id=user.id,
                model=MODEL,
                prompt_version=f"{PROMPT_VERSION}-next",
                now=NOW,
            )
        ).hash != payload.hash


class TestTheInterestedCap:
    async def test_only_the_most_recently_added_survive(self, session, make_user):
        user = await make_user()
        session.add_all(
            [
                Show(id=970_000 + n, name=f"Show {n:03d}", first_air_date=AIRED, status="Ended")
                for n in range(INTERESTED_CAP + 10)
            ]
        )
        await session.flush()
        for n in range(INTERESTED_CAP + 10):
            await _track(session, user.id, 970_000 + n, added_at=NOW - timedelta(days=n))

        payload = await build_payload(session, user_id=user.id, model=MODEL, now=NOW)
        document = _document(payload)

        assert payload.interested_count == INTERESTED_CAP
        assert len(document["interested"]) == INTERESTED_CAP
        # What the cap dropped, which the rows alone cannot report: a tier at
        # exactly 50 reads the same however many were bookmarked (NEU-1105).
        assert payload.interested_before_cap == INTERESTED_CAP + 10
        # Added n days ago, so 0..49 survive and 50..59 are dropped.
        assert {row[0] for row in document["interested"]} == {
            f"Show {n:03d}" for n in range(INTERESTED_CAP)
        }

    async def test_a_capped_show_is_still_excluded(self, session, make_user):
        """The rows are what the model is told; the exclusion set is the rule."""
        user = await make_user()
        session.add_all(
            [
                Show(id=971_000 + n, name=f"Show {n:03d}", first_air_date=AIRED, status="Ended")
                for n in range(INTERESTED_CAP + 10)
            ]
        )
        await session.flush()
        for n in range(INTERESTED_CAP + 10):
            await _track(session, user.id, 971_000 + n, added_at=NOW - timedelta(days=n))

        payload = await build_payload(session, user_id=user.id, model=MODEL, now=NOW)

        assert len(payload.excluded_show_ids) == INTERESTED_CAP + 10

    async def test_a_capped_show_is_named_in_the_exclude_group(self, session, make_user):
        """The rule is only followable if the model can see what it covers.

        The overflow used to be excluded and invisible, so the model was asked to
        avoid rows it was never shown — which is how a 2026-08-17 production run
        named 25 of 25 titles already in its own input.
        """
        user = await make_user()
        session.add_all(
            [
                Show(id=972_000 + n, name=f"Show {n:03d}", first_air_date=AIRED, status="Ended")
                for n in range(INTERESTED_CAP + 10)
            ]
        )
        await session.flush()
        for n in range(INTERESTED_CAP + 10):
            await _track(session, user.id, 972_000 + n, added_at=NOW - timedelta(days=n))

        payload = await build_payload(session, user_id=user.id, model=MODEL, now=NOW)
        document = _document(payload)

        # The ten the cap dropped, and only those: the surviving 50 are already
        # visible as `interested` rows.
        assert payload.excluded_row_count == 10
        assert {row[0] for row in document["exclude"]} == {
            f"Show {n:03d}" for n in range(INTERESTED_CAP, INTERESTED_CAP + 10)
        }
        assert [row[1] for row in document["exclude"]] == [AIRED.year] * 10


class TestExclusion:
    async def test_an_episode_rating_alone_excludes_a_show_it_cannot_label(
        self, session, make_user, shows
    ):
        """`taste_for_user` will not let an episode rating enrol a show. "We have
        no opinion" and "they have never seen it" are different claims, and only
        the second licenses a recommendation."""
        user = await make_user()
        await episode_rating_repo.upsert(
            session, user_id=user.id, episode_id=EPISODE_RATED + 1, stars=Decimal("5.0")
        )
        await session.flush()

        payload = await build_payload(session, user_id=user.id, model=MODEL, now=NOW)

        assert payload.excluded_show_ids == {EPISODE_RATED}
        # And it is named, because no tier row mentions it — the whole point of
        # the group is that an unfollowable ban is not a ban.
        assert payload.excluded_row_count == 1
        assert _document(payload)["exclude"][0][0] == "Episode Rated"
        assert _document(payload)["liked"] == []

    async def test_an_unlabelled_show_is_excluded_without_being_reported(
        self, session, make_user, shows
    ):
        """Watched, barely, recently, and not in My Shows: no tier in §5.1 covers
        it, and it must still never be recommended back."""
        user = await make_user()
        await episode_watch_repo.bulk_mark(
            session, user_id=user.id, episode_ids=[FIRST + 1], watched_at=RECENTLY
        )
        await session.flush()

        payload = await build_payload(session, user_id=user.id, model=MODEL, now=NOW)
        document = _document(payload)

        assert payload.excluded_show_ids == {FIRST}
        assert document["liked"] == document["not_liked"] == document["interested"] == []

    async def test_another_users_history_never_enters(self, session, make_user, shows):
        user = await make_user()
        other = await make_user(email="other@example.com")
        await _track(session, other.id, THIRD)

        payload = await build_payload(session, user_id=user.id, model=MODEL, now=NOW)

        assert payload.excluded_show_ids == frozenset()

    async def test_the_set_is_identical_to_the_python_union_it_replaces(
        self, session, make_user, shows
    ):
        """NEU-1175 moved the exclusion set from a Python union of the taste
        signals' keys and the episode-rated shows into one query. This is what
        backs the decision not to bump `PROMPT_VERSION`: same members, so the same
        `exclude` rows, so the same bytes and the same hash.

        The union is written out here rather than imported, because the point is
        that the *old* expression and the new one agree. Seed all four sources —
        a share of them on shows the taste tiers cover and a share on shows they
        do not.

        It is an argument, not a proof: agreement is asserted over these rows,
        not over every account. If some real account does turn out to differ, its
        hash changes and it regenerates on its own next Sunday — self-healing,
        and the reason §4.5 could decline the bump.
        """
        user = await make_user()
        await _track(session, user.id, FIRST)
        await _track(session, user.id, SECOND)
        await episode_watch_repo.bulk_mark(
            session, user_id=user.id, episode_ids=[THIRD + 1, THIRD + 2], watched_at=RECENTLY
        )
        await show_rating_repo.upsert(
            session, user_id=user.id, show_id=REBOOT_OLD, stars=Decimal("4.0")
        )
        await episode_rating_repo.upsert(
            session, user_id=user.id, episode_id=EPISODE_RATED + 1, stars=Decimal("5.0")
        )
        await session.flush()

        payload = await build_payload(session, user_id=user.id, model=MODEL, now=NOW)

        signals = await taste_for_user(session, user_id=user.id, now=NOW)
        episode_stars = await episode_rating_repo.mean_stars_per_show_for_user(
            session, user_id=user.id
        )
        assert payload.excluded_show_ids == frozenset(signals) | frozenset(episode_stars)
        # And it really is all five, not a subset the old expression also missed.
        assert payload.excluded_show_ids == {FIRST, SECOND, THIRD, REBOOT_OLD, EPISODE_RATED}

    async def test_the_exclusion_set_costs_one_query_like_the_call_it_replaced(
        self, session, make_user, shows, test_engine
    ):
        """AC 7: a swap, not an addition.

        Pinning an absolute number here would pin `taste_for_user`'s query count
        too, which is not this ticket's to fix in place. What the swap claims is
        narrower: on top of the taste signals, the builder spends one query for
        the exclusion set and one for the titles — where before it spent one for
        the episode-rated shows and one for the titles.
        """
        from sqlalchemy import event

        user = await make_user()
        await _track(session, user.id, FIRST)
        await episode_rating_repo.upsert(
            session, user_id=user.id, episode_id=EPISODE_RATED + 1, stars=Decimal("5.0")
        )
        await session.commit()

        statements: list[str] = []

        def _record(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        engine = test_engine.sync_engine
        event.listen(engine, "before_cursor_execute", _record)
        try:
            await taste_for_user(session, user_id=user.id, now=NOW)
            taste_queries = len(statements)
            statements.clear()
            await build_payload(session, user_id=user.id, model=MODEL, now=NOW)
            payload_queries = len(statements)
        finally:
            event.remove(engine, "before_cursor_execute", _record)

        assert payload_queries == taste_queries + 2


class TestTheFloor:
    async def test_a_user_below_it_is_gated(self, session, make_user, shows):
        user = await make_user()
        for show_id in (FIRST, SECOND):
            await _track(session, user.id, show_id)

        payload = await build_payload(session, user_id=user.id, model=MODEL, now=NOW)

        assert payload.interested_count == 2
        assert payload.meets_floor is False

    async def test_five_liked_shows_clear_it(self, session, make_user):
        user = await make_user()
        session.add_all(
            [
                Show(id=972_000 + n, name=f"Liked {n}", first_air_date=AIRED, status="Ended")
                for n in range(5)
            ]
        )
        await session.flush()
        for n in range(5):
            await show_rating_repo.upsert(
                session, user_id=user.id, show_id=972_000 + n, stars=Decimal("5.0")
            )
        await session.flush()

        payload = await build_payload(session, user_id=user.id, model=MODEL, now=NOW)

        assert payload.liked_count == 5
        assert payload.meets_floor is True


class TestDismissal:
    """NEU-1178's fifth source, through the payload (AC 3, 4, 5).

    A dismissal is an exclusion and deliberately not a taste signal, so what is
    asserted is where it lands (`exclude`, never a tier), what it leaves alone
    (every count and the floor), and that it moves the hash.
    """

    async def _dismiss(self, session, user_id, show_id: int) -> None:
        session.add(UserRecommendationDismissal(user_id=user_id, show_id=show_id))
        await session.flush()

    async def test_a_dismissed_show_lands_in_exclude_and_in_no_tier(
        self, session, make_user, shows
    ):
        """AC 3. The user has no record of this show at all — a dismissal alone
        excludes it, which is what the endpoint's "never recommended" case is."""
        user = await make_user()
        await self._dismiss(session, user.id, THIRD)

        payload = await build_payload(session, user_id=user.id, model=MODEL, now=NOW)
        document = _document(payload)

        assert payload.excluded_show_ids == {THIRD}
        assert document["exclude"] == [["Apples", AIRED.year]]
        assert document["liked"] == document["not_liked"] == document["interested"] == []

    async def test_a_dismissal_moves_no_taste_count_and_no_floor(self, session, make_user, shows):
        """AC 4: it is not a taste signal, so `taste_for_user` never sees it."""
        user = await make_user()
        for show_id in (FIRST, SECOND):
            await _track(session, user.id, show_id)
        before = await build_payload(session, user_id=user.id, model=MODEL, now=NOW)

        await self._dismiss(session, user.id, THIRD)
        after = await build_payload(session, user_id=user.id, model=MODEL, now=NOW)

        assert (after.liked_count, after.interested_count, after.interested_before_cap) == (
            before.liked_count,
            before.interested_count,
            before.interested_before_cap,
        )
        assert after.meets_floor is before.meets_floor
        assert after.excluded_show_ids == before.excluded_show_ids | {THIRD}

    async def test_a_dismissal_moves_the_hash(self, session, make_user, shows):
        """AC 5: the bytes change, so the regeneration gate does not skip this
        user as unchanged — and only users who dismissed something regenerate."""
        user = await make_user()
        await _track(session, user.id, FIRST)
        before = await build_payload(session, user_id=user.id, model=MODEL, now=NOW)

        await self._dismiss(session, user.id, THIRD)
        after = await build_payload(session, user_id=user.id, model=MODEL, now=NOW)

        assert after.hash != before.hash

    async def test_a_dismissed_show_that_also_reached_a_tier_stays_in_its_tier(
        self, session, make_user, shows
    ):
        """`exclude` is `excluded - shown_ids`, so nothing special is needed for
        a show the user both tracks and dismissed: it is visible as itself."""
        user = await make_user()
        await _track(session, user.id, THIRD)
        await self._dismiss(session, user.id, THIRD)

        payload = await build_payload(session, user_id=user.id, model=MODEL, now=NOW)
        document = _document(payload)

        assert document["exclude"] == []
        assert [row[0] for row in document["interested"]] == ["Apples"]
        assert payload.excluded_show_ids == {THIRD}

    async def test_another_users_dismissal_never_enters(self, session, make_user, shows):
        """AC 8, at the payload end."""
        user = await make_user()
        other = await make_user(email="other-dismisser@example.com")
        await self._dismiss(session, other.id, THIRD)

        payload = await build_payload(session, user_id=user.id, model=MODEL, now=NOW)

        assert payload.excluded_show_ids == frozenset()

    async def test_the_exclusion_set_still_costs_one_query(
        self, session, make_user, shows, test_engine
    ):
        """AC 13's payload half: the fifth source is a `union_all` branch, not a
        second round trip."""
        from sqlalchemy import event

        user = await make_user()
        await _track(session, user.id, FIRST)
        await self._dismiss(session, user.id, THIRD)
        await session.commit()

        statements: list[str] = []

        def _record(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        engine = test_engine.sync_engine
        event.listen(engine, "before_cursor_execute", _record)
        try:
            await taste_for_user(session, user_id=user.id, now=NOW)
            taste_queries = len(statements)
            statements.clear()
            await build_payload(session, user_id=user.id, model=MODEL, now=NOW)
            payload_queries = len(statements)
        finally:
            event.remove(engine, "before_cursor_execute", _record)

        assert payload_queries == taste_queries + 2
