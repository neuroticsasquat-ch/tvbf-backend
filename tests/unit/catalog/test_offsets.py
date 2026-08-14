"""The airdate offset's read rules, with no database in sight (NEU-1145).

Three decisions live in `catalog/offsets.py` and nowhere else, and each is easy
to undo by accident: which offset applies to a season, what a writer stores when
one does, and which season the two show-grain dates take theirs from.
"""

from datetime import date

from tvbf.catalog.offsets import EMPTY, FIRST_SEASON, OffsetTable, season_of_last_dated


def test_a_numbered_season_overrides_the_show_wide_default():
    """The whole of the override rule. The default is the operator's escape
    hatch and the numbered rows are the job's, so the job's answer for a season
    it has evidence about must win."""
    offsets = OffsetTable({None: 1, 3: -1})
    assert offsets.for_season(3) == -1
    assert offsets.for_season(1) == 1


def test_an_unknown_season_falls_through_to_the_default():
    """So an operator can correct a show whose seasons are still arriving
    without having to list them."""
    assert OffsetTable({None: 1}).for_season(99) == 1


def test_a_season_with_neither_a_row_nor_a_default_is_uncorrected():
    assert OffsetTable({2: 1}).for_season(5) == 0
    assert EMPTY.for_season(1) == 0


def test_pair_leaves_an_uncorrected_value_alone_and_its_twin_null():
    """NULL in a `tmdb_*` column has to mean *this row is untouched TMDB*. A
    writer that stored the raw value unconditionally would make the "every row
    we altered" predicate match all ~6.5M episodes."""
    assert EMPTY.pair(date(2026, 1, 27), 1) == (date(2026, 1, 27), None)


def test_pair_stores_the_corrected_value_and_the_raw_one_together():
    offsets = OffsetTable({1: 1})
    assert offsets.pair(date(2026, 1, 27), 1) == (date(2026, 1, 28), date(2026, 1, 27))


def test_pair_of_a_missing_date_is_two_nulls():
    """An undated episode is not a corrected one, whatever the season's offset."""
    assert OffsetTable({1: 1}).pair(None, 1) == (None, None)


def test_the_show_premiere_takes_season_ones_offset():
    """Ted Lasso is the proof a blanket per-show shift is wrong: its seasons 1-2
    carry the Eastern date and 3-4 the Pacific, so a premiere that is already
    right must not move while the last-aired date must."""
    offsets = OffsetTable({3: 1, 4: 1})
    assert offsets.for_season(FIRST_SEASON) == 0
    assert offsets.for_season(4) == 1


def test_season_of_last_dated_picks_the_latest_airdate():
    assert season_of_last_dated([(1, date(2020, 8, 14)), (4, date(2026, 3, 4))]) == 4


def test_season_of_last_dated_ignores_undated_seasons():
    """A show whose newest season has no dates yet has not aired from it, so the
    last-aired date still belongs to the season before."""
    assert season_of_last_dated([(2, date(2024, 1, 1)), (3, None)]) == 2


def test_season_of_last_dated_is_none_when_nothing_is_dated():
    assert season_of_last_dated([(1, None)]) is None
    assert season_of_last_dated([]) is None


def test_a_tie_goes_to_the_higher_season():
    """Two seasons dated the same day is a two-part premiere or a compilation;
    the higher number is the one a viewer would call current."""
    assert season_of_last_dated([(1, date(2026, 1, 1)), (2, date(2026, 1, 1))]) == 2
