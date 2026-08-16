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

from tvbf.app.models import UserShowWatch
from tvbf.app.repos import episode_rating_repo, episode_watch_repo, show_rating_repo
from tvbf.catalog.models import Episode, Show
from tvbf.recommendations.payload import INTERESTED_CAP, build_payload, payload_hash

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
        """Shipping a prompt change re-runs everyone exactly once (§9.1)."""
        user = await make_user()
        await _track(session, user.id, THIRD)

        payload = await build_payload(session, user_id=user.id, model=MODEL, now=NOW)

        assert payload.hash == payload_hash(
            prompt_version="1", model=MODEL, canonical_json=payload.json
        )
        assert (
            await build_payload(session, user_id=user.id, model="other", now=NOW)
        ).hash != payload.hash
        assert (
            await build_payload(session, user_id=user.id, model=MODEL, prompt_version="2", now=NOW)
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
