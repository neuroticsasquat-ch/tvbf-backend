"""Window planning and cursor encoding for the catalog delta (NEU-1035).

Pure functions, and the two places the delta can lose data silently: a gap
walked in windows that skip a day, and a cursor that does not survive a round
trip through the column it shares with TV Maze's epochs.
"""

from datetime import UTC, date, datetime, timedelta

import pytest

from tvbf.tmdb.client import CHANGES_MAX_WINDOW_DAYS
from tvbf.tmdb.update import cursor_to_date, date_to_cursor, plan_windows, split_window


def test_a_thirty_day_gap_becomes_consecutive_windows_none_too_wide():
    """The ticket's acceptance criterion: never one oversized request."""
    start, end = date(2026, 6, 1), date(2026, 7, 1)

    windows = plan_windows(start, end)

    assert [(s.isoformat(), e.isoformat()) for s, e in windows] == [
        ("2026-06-01", "2026-06-15"),
        ("2026-06-15", "2026-06-29"),
        ("2026-06-29", "2026-07-01"),
    ]
    assert all((e - s).days <= CHANGES_MAX_WINDOW_DAYS for s, e in windows)


def test_the_windows_cover_the_whole_gap_with_no_hole():
    """Each window begins where the last ended. TMDB's bounds are inclusive, so
    a disjoint split would drop every change dated on a boundary day."""
    windows = plan_windows(date(2026, 1, 1), date(2026, 3, 15))

    assert windows[0][0] == date(2026, 1, 1)
    assert windows[-1][1] == date(2026, 3, 15)
    assert all(prev[1] == nxt[0] for prev, nxt in zip(windows, windows[1:], strict=False))


def test_an_ordinary_day_is_a_single_window():
    assert plan_windows(date(2026, 8, 9), date(2026, 8, 10)) == [
        (date(2026, 8, 9), date(2026, 8, 10))
    ]


def test_exactly_the_maximum_span_stays_one_request():
    start = date(2026, 8, 1)
    assert plan_windows(start, start + timedelta(days=CHANGES_MAX_WINDOW_DAYS)) == [
        (start, start + timedelta(days=CHANGES_MAX_WINDOW_DAYS))
    ]


@pytest.mark.parametrize(
    "start, end",
    [
        (date(2026, 8, 10), date(2026, 8, 10)),  # already caught up
        (date(2026, 8, 11), date(2026, 8, 10)),  # a cursor ahead of today
    ],
)
def test_nothing_to_cover_plans_no_request(start, end):
    """A backwards range is one TMDB has no answer for, and a clock adjustment
    is enough to produce one."""
    assert plan_windows(start, end) == []


def test_a_window_must_span_at_least_a_day():
    with pytest.raises(ValueError, match="at least a day"):
        plan_windows(date(2026, 1, 1), date(2026, 2, 1), max_days=0)


def test_a_window_halves_into_two_that_still_meet():
    """The overflow path reuses `plan_windows`'s shared-boundary rule — halves
    that abutted rather than met would drop the seam day's changes."""
    halves = split_window(date(2026, 7, 27), date(2026, 8, 10))

    assert halves == [
        (date(2026, 7, 27), date(2026, 8, 3)),
        (date(2026, 8, 3), date(2026, 8, 10)),
    ]


def test_a_single_day_has_no_halves_left():
    """What tells the caller to fail rather than truncate."""
    assert split_window(date(2026, 8, 9), date(2026, 8, 10)) is None


def test_halving_terminates_at_a_day():
    """Every split must shrink, or the overflow path loops forever."""
    start, end = date(2026, 1, 1), date(2026, 1, 15)
    while (halves := split_window(start, end)) is not None:
        assert all(s < e for s, e in halves)
        start, end = halves[0]
    assert (end - start).days == 1


def test_the_cursor_round_trips_through_the_shared_column():
    """The lineage stores a date in a column typed for a TV Maze epoch. That is
    only safe because the encoding is exact in both directions."""
    for day in (date(2026, 1, 1), date(2026, 8, 10), date(2026, 12, 31)):
        assert cursor_to_date(date_to_cursor(day)) == day


def test_the_cursor_is_midnight_utc_not_local_midnight():
    """A local-midnight epoch would decode to the day before or after depending
    on where the container runs, which is a day of changes lost or repeated."""
    assert datetime.fromtimestamp(date_to_cursor(date(2026, 8, 10)), tz=UTC) == datetime(
        2026, 8, 10, tzinfo=UTC
    )
