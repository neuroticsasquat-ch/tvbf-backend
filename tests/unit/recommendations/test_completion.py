"""The percentage arithmetic, without a database (project spec §12)."""

from datetime import UTC, datetime

import pytest

from tvbf.recommendations.completion import ShowCompletion, completion_pct


def pct(watched: int, aired: int) -> int:
    return completion_pct(watched_episodes=watched, aired_episodes=aired)


class TestEndpoints:
    """0 and 100 are claims about taste, so nothing rounds into either."""

    def test_nothing_watched_is_zero(self):
        assert pct(0, 20) == 0

    def test_nothing_aired_is_zero(self):
        """A show announced but not started: the user cannot be behind on it."""
        assert pct(0, 0) == 0

    def test_every_aired_episode_watched_is_a_hundred(self):
        assert pct(20, 20) == 100

    def test_caught_up_on_an_airing_show_is_a_hundred(self):
        """The whole ticket: 4 of 4 aired reads 100 even though 10 are ordered."""
        assert pct(4, 4) == 100

    def test_more_watched_than_aired_saturates(self):
        """An unaired or undated episode marked watched must not exceed 100."""
        assert pct(6, 4) == 100

    def test_one_episode_in_is_not_zero(self):
        assert pct(1, 500) == 1

    def test_one_episode_short_is_not_a_hundred(self):
        assert pct(499, 500) == 99


class TestInBetween:
    @pytest.mark.parametrize(
        ("watched", "aired", "expected"),
        [
            (1, 2, 50),
            (1, 8, 12),
            (3, 4, 75),
            (2, 3, 66),  # rounds down, never up
            (1, 3, 33),
        ],
    )
    def test_rounds_down(self, watched: int, aired: int, expected: int):
        assert pct(watched, aired) == expected

    def test_the_tier_boundary_is_reachable_exactly(self):
        """NEU-1103 splits on `>= 50`, so half of an even run must read 50."""
        assert pct(5, 10) == 50
        assert pct(4, 10) == 40


class TestShowCompletion:
    def test_pct_is_derived_from_the_counts(self):
        row = ShowCompletion(watched_episodes=3, aired_episodes=4, last_watched_at=None)
        assert row.pct == 75

    def test_last_watched_at_is_carried_verbatim(self):
        when = datetime(2019, 3, 1, tzinfo=UTC)
        row = ShowCompletion(watched_episodes=1, aired_episodes=40, last_watched_at=when)
        assert row.last_watched_at == when

    def test_a_never_started_show_reads_zero(self):
        row = ShowCompletion(watched_episodes=0, aired_episodes=0, last_watched_at=None)
        assert row.pct == 0
        assert row.last_watched_at is None
