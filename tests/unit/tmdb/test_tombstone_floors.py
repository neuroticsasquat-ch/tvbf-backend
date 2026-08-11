"""The tombstone plausibility floors (NEU-1036), calibrated against 228,611.

The relative floor cannot be reached from an integration test — tripping it
needs more ids than any test can seed rows for — so the predicate is exercised
directly here. It is the guard between a bad download and a fully tombstoned
catalog, so "untestable in practice" is not an acceptable state for it.
"""

from tvbf.tmdb.tombstone import (
    _MEASURED_EXPORT,
    _MIN_FEED_ABSOLUTE,
    _MIN_FEED_RELATIVE,
    _feed_is_implausible,
)


def test_the_absolute_floor_sits_well_under_the_measured_export():
    """Far enough below to never fire on a healthy day, far enough above zero to
    catch a badly truncated one."""
    assert _MIN_FEED_ABSOLUTE < _MEASURED_EXPORT
    assert 0.5 < _MIN_FEED_ABSOLUTE / _MEASURED_EXPORT < 0.8


def test_a_realistic_export_against_a_full_mirror_is_trusted():
    assert _feed_is_implausible(_MEASURED_EXPORT, _MEASURED_EXPORT) is None


def test_an_export_one_id_short_of_the_absolute_floor_is_refused():
    reason = _feed_is_implausible(_MIN_FEED_ABSOLUTE - 1, 0)

    assert reason is not None
    assert "absolute floor" in reason


def test_an_export_that_lost_a_tenth_of_a_full_mirror_is_refused():
    """Clears the absolute floor, so only the relative one catches it. Upstream
    does not shed 5% of its catalog in a day."""
    reason = _feed_is_implausible(int(_MEASURED_EXPORT * 0.9), _MEASURED_EXPORT)

    assert reason is not None
    assert "known" in reason


def test_a_short_export_is_refused_even_when_the_mirror_is_far_smaller():
    """The migration-window hole, and the reason the denominator is a maximum.

    Before cutover the mirror holds ~63k mapped rows against a ~229k export, so
    a floor of `95% of mirrored` is ~60k — no floor at all. An export carrying
    two thirds of the catalog would otherwise clear both guards and tombstone
    every mapped series in the missing third.
    """
    two_thirds = int(_MEASURED_EXPORT * 0.66)
    assert two_thirds > _MIN_FEED_ABSOLUTE, "the absolute floor must not be what catches this"

    reason = _feed_is_implausible(two_thirds, 63_000)

    assert reason is not None
    assert "known" in reason


def test_a_full_export_is_trusted_while_the_mirror_is_still_filling():
    """The state the migration is actually in. A guard that fired here would
    block tombstoning until cutover."""
    assert _feed_is_implausible(_MEASURED_EXPORT, 63_000) is None


def test_a_mirror_larger_than_the_measurement_takes_over_as_the_denominator():
    """The constant is a floor under the estimate, not a ceiling on it — a
    catalog that has grown since 2026-08-07 is judged against itself."""
    grown = _MEASURED_EXPORT * 2

    assert _feed_is_implausible(_MEASURED_EXPORT, grown) is not None
    assert _feed_is_implausible(grown, grown) is None


def test_the_relative_floor_is_a_fraction_not_a_multiplier():
    """Guards a sign error nothing else would catch: at >1 every export fails."""
    assert 0 < _MIN_FEED_RELATIVE < 1
