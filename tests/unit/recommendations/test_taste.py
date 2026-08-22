"""The tier rules, without a database (project spec §12).

Every case below is one row of §5.1's two tables, or one of the seams between
them: the dead band, the 180-day boundary, and the shows no rule covers.
"""

from datetime import UTC, datetime, timedelta

import pytest

from tvbf.recommendations.completion import ShowCompletion
from tvbf.recommendations.taste import (
    ABANDONED_AFTER_DAYS,
    TasteLabel,
    classify,
)

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
RECENTLY = NOW - timedelta(days=3)
LONG_AGO = NOW - timedelta(days=ABANDONED_AFTER_DAYS + 1)


def completion(
    *, watched: int = 0, aired: int = 10, last_watched_at: datetime | None = None
) -> ShowCompletion:
    return ShowCompletion(
        watched_episodes=watched,
        aired_episodes=aired,
        last_watched_at=last_watched_at,
    )


def label(
    *,
    watched: int = 0,
    aired: int = 10,
    last_watched_at: datetime | None = None,
    in_my_shows: bool = False,
    stars: float | None = None,
) -> TasteLabel | None:
    return classify(
        completion=completion(watched=watched, aired=aired, last_watched_at=last_watched_at),
        in_my_shows=in_my_shows,
        stars=stars,
        now=NOW,
    )


class TestRatingOverride:
    """Applied first, and only at the ends of the range."""

    @pytest.mark.parametrize("stars", [3.5, 4.0, 4.5, 5.0])
    def test_high_stars_are_liked_however_little_was_watched(self, stars: float):
        assert (
            label(stars=stars, watched=1, aired=100, last_watched_at=LONG_AGO) is TasteLabel.LIKED
        )

    @pytest.mark.parametrize("stars", [0.5, 1.0, 1.5, 2.0])
    def test_low_stars_are_not_liked_however_much_was_watched(self, stars: float):
        assert (
            label(stars=stars, watched=10, aired=10, in_my_shows=True, last_watched_at=RECENTLY)
            is TasteLabel.NOT_LIKED
        )

    @pytest.mark.parametrize("stars", [2.5, 3.0])
    def test_the_dead_band_overrides_nothing(self, stars: float):
        """3 stars means "it was fine"; completion is the better read of fine, so
        a finished show stays LIKED and an abandoned one stays NOT LIKED."""
        assert (
            label(stars=stars, watched=10, aired=10, last_watched_at=RECENTLY) is TasteLabel.LIKED
        )
        assert (
            label(stars=stars, watched=1, aired=100, last_watched_at=LONG_AGO)
            is TasteLabel.NOT_LIKED
        )

    def test_a_dead_band_rating_on_an_untouched_show_still_reaches_interested(self):
        assert label(stars=3.0, in_my_shows=True) is TasteLabel.INTERESTED

    def test_a_synthesized_mean_is_judged_where_it_falls(self):
        """The mean of episode ratings is continuous — it does not have to be one
        of the ten values a user could type, and nothing rounds it onto one."""
        assert label(stars=3.4, in_my_shows=True) is TasteLabel.INTERESTED
        assert label(stars=3.6, in_my_shows=True) is TasteLabel.LIKED


class TestLiked:
    def test_half_a_show_is_liked_without_membership(self):
        """Completion outranks membership: the 8 watched-but-not-added rows in
        the baseline are still a positive signal."""
        assert label(watched=5, aired=10, last_watched_at=LONG_AGO) is TasteLabel.LIKED

    def test_membership_plus_one_watched_episode_is_liked(self):
        assert (
            label(watched=1, aired=100, in_my_shows=True, last_watched_at=RECENTLY)
            is TasteLabel.LIKED
        )

    def test_half_a_show_is_liked_even_when_it_was_abandoned(self):
        """Completion outranks the abandonment clause as well as membership:
        somebody who watched half of it told us something a stale date does not
        take back."""
        assert label(watched=5, aired=10, last_watched_at=LONG_AGO) is TasteLabel.LIKED

    def test_forty_nine_percent_is_not_enough_on_its_own(self):
        """The boundary from below: without membership or a rating, just under
        half is not LIKED."""
        assert label(watched=49, aired=100, last_watched_at=RECENTLY) is None

    def test_watching_only_specials_is_not_watching_it(self):
        """`watched_episodes` strips specials while `last_watched_at` does not —
        the pair means "they were here, but no regular episode moved"."""
        assert label(watched=0, aired=10, in_my_shows=True, last_watched_at=RECENTLY) is (
            TasteLabel.INTERESTED
        )


class TestNotLiked:
    def test_started_and_dropped_is_not_liked(self):
        assert label(watched=1, aired=100, last_watched_at=LONG_AGO) is TasteLabel.NOT_LIKED

    def test_an_abandoned_my_shows_row_is_not_liked(self):
        """The overlap between the two tiers. Membership does not rescue a show
        nobody has touched in two years — behaviour outranks it, and the other
        reading leaves NOT LIKED with only the 8 un-added rows to describe."""
        assert (
            label(watched=1, aired=100, in_my_shows=True, last_watched_at=LONG_AGO)
            is TasteLabel.NOT_LIKED
        )

    def test_the_boundary_day_counts_as_abandoned(self):
        exactly = NOW - timedelta(days=ABANDONED_AFTER_DAYS)
        assert label(watched=1, aired=100, last_watched_at=exactly) is TasteLabel.NOT_LIKED

    def test_a_show_started_this_week_is_not_abandoned(self):
        """The whole point of the clause: recent and stale are different facts."""
        assert label(watched=1, aired=100, last_watched_at=RECENTLY) is None


class TestInterested:
    def test_my_shows_with_nothing_watched(self):
        assert label(in_my_shows=True) is TasteLabel.INTERESTED

    def test_a_watchlisted_show_that_has_not_aired_yet(self):
        assert label(in_my_shows=True, aired=0) is TasteLabel.INTERESTED


class TestUncovered:
    """Shows no rule in §5.1 reaches. `None`, not a widened tier."""

    def test_untracked_barely_watched_and_recent(self):
        assert label(watched=1, aired=100, last_watched_at=RECENTLY) is None

    def test_a_show_the_user_has_done_nothing_with(self):
        assert label() is None

    def test_a_dead_band_rating_on_a_show_that_is_otherwise_untouched(self):
        assert label(stars=2.5) is None
