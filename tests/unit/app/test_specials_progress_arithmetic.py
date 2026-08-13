"""The percentage a user sees, before and after NEU-1062 — including the case
where it goes *down*.

Excluding specials from both halves of the fraction raises the percentage for
every user-show pair in production (18 of them move, nine landing on exactly
100%). But that is a property of the data, not of the arithmetic, and the shape
that would surprise someone later belongs where a reader will meet it rather
than in a runbook nobody re-reads.

`WatchProgressBar` computes `Math.round((watched / aired) * 100)` from
`watched_episode_count` / `aired_episode_count`, so `_pct` below is the
frontend's formula, held here against the counts the backend now supplies.
"""


def _pct(watched: int, aired: int) -> int | None:
    """The SPA's progress bar. `None` where it renders no bar at all."""
    if aired <= 0:
        return None
    return round((watched / aired) * 100)


class TestTheTicketsCase:
    def test_every_regular_episode_and_no_specials_is_one_hundred_percent(self):
        # 10 regular aired, 5 aired specials, all 10 regulars watched.
        assert _pct(10, 15) == 67  # before
        assert _pct(10, 10) == 100  # after

    def test_watching_specials_too_cannot_exceed_one_hundred(self):
        # All 15 watched. The numerator drops with the denominator.
        assert _pct(15, 15) == 100
        assert _pct(10, 10) == 100

    def test_saturday_night_live_rises_despite_eighty_nine_watched_specials(self):
        """The production pair with the most special watches by far, and it
        still goes up: 1,008 of 1,009 regulars are watched too."""
        assert _pct(1097, 1325) == 83
        assert _pct(1008, 1009) == 100


class TestThePercentageCanFall:
    def test_a_user_who_watched_mostly_specials_reads_lower_afterwards(self):
        """2 of 10 regular aired episodes watched, plus all 5 aired specials.

        Does not occur in production data — no pair regressed — but it is the
        shape of the arithmetic rather than an accident of the data, and a
        reader meeting it here will not mistake a later report for a bug.
        """
        assert _pct(7, 15) == 47  # before: 2 regulars + 5 specials over 15
        assert _pct(2, 10) == 20  # after: only the regulars count, either side

    def test_a_pair_watched_only_through_specials_falls_to_zero(self):
        """Zero such pairs exist in production, which is what makes the
        consequence acceptable: `list_watched` skips `watched == 0`, so such a
        show would drop out of the Watched library entirely."""
        assert _pct(5, 15) == 33
        assert _pct(0, 10) == 0


class TestAShowWithNoRegularEpisodes:
    """357 shows are 100% specials. Their denominator is 0 after the change."""

    def test_it_has_no_progress_rather_than_zero_percent(self):
        assert _pct(0, 0) is None

    def test_watching_all_of_its_specials_changes_nothing(self):
        assert _pct(0, 0) is None
