"""The plausibility floors guarding the tombstone reverse diff (NEU-1005).

Tested here rather than through the DB because the relative floor only engages
on a mirror large enough that 95% of it still clears the absolute floor — over
50,000 rows. Seeding that to assert a pure predicate would be slow and would
test the seeding, not the guard. The "writes nothing when it trips" behaviour is
shared by both branches and is covered in the integration tests.
"""

import pytest

from tvbf.tvmaze.tombstone import (
    _MIN_FEED_ABSOLUTE,
    _MIN_FEED_RELATIVE,
    _feed_is_implausible,
)


def test_a_full_feed_is_plausible():
    assert _feed_is_implausible(89_000, 88_971) is None


def test_a_feed_slightly_ahead_of_the_mirror_is_plausible():
    """The normal steady state: upstream has shows we haven't ingested yet.

    Measured on prod 2026-08-06 — 88,997 upstream against 88,971 mirrored.
    """
    assert _feed_is_implausible(88_997, 88_971) is None


@pytest.mark.parametrize("feed_size", [0, 1, _MIN_FEED_ABSOLUTE - 1])
def test_a_feed_under_the_absolute_floor_is_implausible(feed_size):
    reason = _feed_is_implausible(feed_size, 88_971)
    assert reason is not None
    assert "absolute floor" in reason


def test_a_feed_under_the_relative_floor_is_implausible():
    """Large enough to clear the absolute floor, still far short of the mirror."""
    mirrored = 89_000
    feed_size = int(mirrored * (_MIN_FEED_RELATIVE - 0.05))
    assert feed_size > _MIN_FEED_ABSOLUTE, "otherwise this exercises the absolute floor"

    reason = _feed_is_implausible(feed_size, mirrored)
    assert reason is not None
    assert "of the mirror" in reason


def test_a_feed_just_above_the_relative_floor_is_plausible():
    mirrored = 89_000
    assert _feed_is_implausible(int(mirrored * _MIN_FEED_RELATIVE) + 1, mirrored) is None


def test_a_feed_just_below_the_relative_floor_is_implausible():
    mirrored = 89_000
    assert _feed_is_implausible(int(mirrored * _MIN_FEED_RELATIVE) - 1, mirrored) is not None


def test_an_empty_mirror_does_not_trip_the_relative_floor():
    """A fresh DB has no shows, so `feed < 0.95 * 0` must not divide or fire.

    The absolute floor still applies, which is what should catch a bad feed here.
    """
    assert _feed_is_implausible(_MIN_FEED_ABSOLUTE + 1, 0) is None
