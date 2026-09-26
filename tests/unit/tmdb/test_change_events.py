"""Catalog change detection's pure comparison (NEU-1481, project spec §5.1).

The old side is a `ShowSnapshot` built by hand and the new side a parsed
`TMDBSeries`, so every kind and every non-event is decided here without a
database. The read and write around it are covered in
`tests/integration/tmdb/test_change_events.py`.
"""

from datetime import date

import pytest

from tests.fixtures.tmdb.series_factory import make_season_summary, make_series
from tvbf.tmdb.api_payloads import TMDBSeries
from tvbf.tmdb.change_events import ShowSnapshot, Transition, detect_transitions

TODAY = date(2026, 9, 26)
FUTURE = "2026-11-01"
LATER = "2026-11-15"
PAST = "2026-01-10"


def _series(status: str | None = "Returning Series", seasons: dict[int, str | None] | None = None):
    payload = make_series(1396, seasons=0, append_seasons=False, status=status)
    payload["seasons"] = [
        make_season_summary(139600 + n, n, air_date=air_date or "")
        for n, air_date in (seasons or {}).items()
    ]
    return TMDBSeries.model_validate(payload)


def _snapshot(status: str | None = "Returning Series", premieres=None) -> ShowSnapshot:
    return ShowSnapshot(
        status=status,
        premieres={n: date.fromisoformat(d) if d else None for n, d in (premieres or {}).items()},
    )


def _detect(old: ShowSnapshot, new: TMDBSeries) -> list[Transition]:
    return detect_transitions(old, new, today=TODAY)


class TestStatus:
    @pytest.mark.parametrize("new_status", ["Ended", "Canceled"])
    def test_entering_a_terminal_status_is_ended(self, new_status):
        assert _detect(_snapshot("Returning Series"), _series(new_status)) == [
            Transition("ended", "Returning Series", new_status)
        ]

    def test_a_null_status_becoming_terminal_is_ended(self):
        assert _detect(_snapshot(None), _series("Ended")) == [Transition("ended", None, "Ended")]

    @pytest.mark.parametrize("old_status", ["Ended", "Canceled"])
    def test_leaving_a_terminal_status_is_revived(self, old_status):
        assert _detect(_snapshot(old_status), _series("Returning Series")) == [
            Transition("revived", old_status, "Returning Series")
        ]

    def test_moving_between_the_two_terminal_statuses_is_nothing(self):
        assert _detect(_snapshot("Ended"), _series("Canceled")) == []

    @pytest.mark.parametrize("old_status", ["Ended", "Returning Series"])
    def test_a_status_going_null_is_nothing(self, old_status):
        assert _detect(_snapshot(old_status), _series(None)) == []

    def test_an_unchanged_status_is_nothing(self):
        assert _detect(_snapshot("Ended"), _series("Ended")) == []


class TestPremiereSet:
    def test_a_null_date_becoming_a_future_one(self):
        assert _detect(_snapshot(premieres={2: None}), _series(seasons={2: FUTURE})) == [
            Transition("premiere_set", None, FUTURE, season_number=2)
        ]

    def test_a_season_row_that_did_not_exist(self):
        assert _detect(_snapshot(premieres={1: PAST}), _series(seasons={1: PAST, 2: FUTURE})) == [
            Transition("premiere_set", None, FUTURE, season_number=2)
        ]

    def test_a_date_arriving_already_in_the_past_is_a_backfill(self):
        assert _detect(_snapshot(premieres={2: None}), _series(seasons={2: PAST})) == []

    def test_a_date_arriving_today_is_not_in_the_future(self):
        assert _detect(_snapshot(premieres={2: None}), _series(seasons={2: "2026-09-26"})) == []

    def test_a_season_zero_date_is_nothing(self):
        assert _detect(_snapshot(premieres={0: None}), _series(seasons={0: FUTURE})) == []

    def test_a_season_still_undated_is_nothing(self):
        assert _detect(_snapshot(premieres={2: None}), _series(seasons={2: None})) == []


class TestPremiereMoved:
    def test_a_future_date_moving_to_another(self):
        assert _detect(_snapshot(premieres={2: FUTURE}), _series(seasons={2: LATER})) == [
            Transition("premiere_moved", FUTURE, LATER, season_number=2)
        ]

    def test_a_past_date_moving_into_the_future(self):
        assert _detect(_snapshot(premieres={2: PAST}), _series(seasons={2: FUTURE})) == [
            Transition("premiere_moved", PAST, FUTURE, season_number=2)
        ]

    def test_a_future_date_pulled_into_the_past(self):
        assert _detect(_snapshot(premieres={2: FUTURE}), _series(seasons={2: PAST})) == [
            Transition("premiere_moved", FUTURE, PAST, season_number=2)
        ]

    def test_a_historical_correction_is_nothing(self):
        assert _detect(_snapshot(premieres={2: PAST}), _series(seasons={2: "2026-01-11"})) == []

    def test_an_unchanged_date_is_nothing(self):
        assert _detect(_snapshot(premieres={2: FUTURE}), _series(seasons={2: FUTURE})) == []

    def test_a_date_withdrawn_to_null_is_nothing(self):
        assert _detect(_snapshot(premieres={2: FUTURE}), _series(seasons={2: None})) == []

    def test_a_season_zero_move_is_nothing(self):
        assert _detect(_snapshot(premieres={0: FUTURE}), _series(seasons={0: LATER})) == []


def test_status_and_premiere_transitions_are_reported_together():
    old = _snapshot("Ended", premieres={1: PAST})
    new = _series("Returning Series", seasons={1: PAST, 2: FUTURE})

    assert _detect(old, new) == [
        Transition("revived", "Ended", "Returning Series"),
        Transition("premiere_set", None, FUTURE, season_number=2),
    ]
