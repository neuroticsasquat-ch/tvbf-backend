from datetime import UTC, date, datetime
from uuid import UUID

import pytest

from tvbf.push.candidates import Candidate, apply_cap, group_by_user, is_still_current

TODAY = date(2026, 9, 26)
ALICE = UUID("00000000-0000-0000-0000-00000000000a")
BOB = UUID("00000000-0000-0000-0000-00000000000b")


# --- is_still_current ---------------------------------------------------------


@pytest.mark.parametrize("kind", ["premiere_set", "premiere_moved"])
def test_a_premiere_is_current_while_its_raw_date_is_unchanged(kind):
    assert is_still_current(
        kind,
        new_value="2026-10-05",
        raw_air_date=date(2026, 10, 5),
        air_date=date(2026, 10, 5),
        is_ended=False,
        today=TODAY,
    )


def test_a_premiere_compares_the_raw_date_not_the_corrected_one():
    # An offset-corrected season: TMDB says the 4th, the app shows the 5th.
    assert is_still_current(
        "premiere_set",
        new_value="2026-10-04",
        raw_air_date=date(2026, 10, 4),
        air_date=date(2026, 10, 5),
        is_ended=False,
        today=TODAY,
    )


@pytest.mark.parametrize("kind", ["premiere_set", "premiere_moved"])
def test_a_premiere_that_moved_again_is_stale(kind):
    assert not is_still_current(
        kind,
        new_value="2026-10-05",
        raw_air_date=date(2026, 10, 19),
        air_date=date(2026, 10, 19),
        is_ended=False,
        today=TODAY,
    )


def test_a_premiere_whose_date_was_removed_is_stale():
    assert not is_still_current(
        "premiere_set",
        new_value="2026-10-05",
        raw_air_date=None,
        air_date=None,
        is_ended=False,
        today=TODAY,
    )


def test_a_premiere_today_is_still_current_but_one_already_aired_is_not():
    def current(corrected: date) -> bool:
        return is_still_current(
            "premiere_moved",
            new_value=corrected.isoformat(),
            raw_air_date=corrected,
            air_date=corrected,
            is_ended=False,
            today=TODAY,
        )

    assert current(TODAY)
    assert not current(date(2026, 9, 25))


@pytest.mark.parametrize(("is_ended", "expected"), [(True, True), (False, False)])
def test_ended_is_current_while_the_show_is_ended(is_ended, expected):
    assert (
        is_still_current(
            "ended",
            new_value="Ended",
            raw_air_date=None,
            air_date=None,
            is_ended=is_ended,
            today=TODAY,
        )
        is expected
    )


@pytest.mark.parametrize(("is_ended", "expected"), [(False, True), (True, False)])
def test_revived_is_current_while_the_show_is_not_ended(is_ended, expected):
    assert (
        is_still_current(
            "revived",
            new_value="Returning Series",
            raw_air_date=None,
            air_date=None,
            is_ended=is_ended,
            today=TODAY,
        )
        is expected
    )


# --- apply_cap ----------------------------------------------------------------


def _airs(user: UUID, show_name: str, show_id: int) -> Candidate:
    """One show's airs-today candidate — one per show since NEU-1539."""
    return Candidate(
        user_id=user,
        kind="airs_today",
        key=f"airs_today:{show_id}:{TODAY}",
        show_id=show_id,
        episode_id=show_id * 100,
        show_name=show_name,
        season_number=1,
        episode_number=1,
    )


def _ev(user: UUID, event_id: int, hour: int, show_name: str = "Zed") -> Candidate:
    return Candidate(
        user_id=user,
        kind="ended",
        key=f"ended:{event_id}",
        event_id=event_id,
        show_name=show_name,
        observed_at=datetime(2026, 9, 26, hour, tzinfo=UTC),
    )


def test_airs_today_come_first_by_show_name_then_events_oldest_first():
    candidates = [
        _ev(ALICE, 2, hour=9),
        _airs(ALICE, "The Wire", 10),
        _ev(ALICE, 1, hour=3),
        _airs(ALICE, "Severance", 11),
        _airs(ALICE, "Andor", 12),
    ]

    capped = apply_cap({ALICE: candidates}, today=TODAY)

    # "The Wire" sorts under W.
    assert [c.key for c in capped[ALICE]] == [
        f"airs_today:12:{TODAY}",
        f"airs_today:11:{TODAY}",
        f"airs_today:10:{TODAY}",
        "ended:1",
        "ended:2",
    ]


def test_events_sharing_observed_at_order_by_event_id():
    capped = apply_cap({ALICE: [_ev(ALICE, 8, hour=1), _ev(ALICE, 7, hour=1)]}, today=TODAY)

    assert [c.event_id for c in capped[ALICE]] == [7, 8]


def test_at_the_cap_nothing_is_summarised():
    candidates = [_airs(ALICE, f"Show {i}", i) for i in range(5)]

    capped = apply_cap({ALICE: candidates}, 5, today=TODAY)

    assert len(capped[ALICE]) == 5
    assert all(c.kind == "airs_today" for c in capped[ALICE])


def test_past_the_cap_the_remainder_becomes_one_summary():
    candidates = [_airs(ALICE, f"Show {i}", i) for i in range(4)] + [
        _ev(ALICE, i, hour=i) for i in range(1, 5)
    ]

    capped = apply_cap({ALICE: candidates}, 5, today=TODAY)

    kept, summary = capped[ALICE][:5], capped[ALICE][5]
    assert [c.kind for c in kept] == ["airs_today"] * 4 + ["ended"]
    assert kept[-1].event_id == 1
    assert summary == Candidate(
        user_id=ALICE,
        kind="summary",
        key=f"summary:{ALICE}:2026-09-26",
        count=3,
        show_names=("Zed",),
    )


def test_a_summary_names_each_overflowing_show_once_in_delivery_order():
    candidates = [
        _airs(ALICE, "Andor", 1),
        _airs(ALICE, "Severance", 2),
        _airs(ALICE, "The Wire", 4),
        _ev(ALICE, 1, hour=3, show_name="Severance"),
        _ev(ALICE, 2, hour=4),
    ]

    summary = apply_cap({ALICE: candidates}, 1, today=TODAY)[ALICE][-1]

    assert summary.count == 4
    assert summary.show_names == ("Severance", "The Wire", "Zed")


def test_a_season_dump_is_one_candidate_against_the_cap():
    """The ticket's case (NEU-1539): the fold happens in `airs_today_candidates`,
    so the cap sees one candidate per show however many episodes it carries."""
    dump = _airs(ALICE, "Severance", 1)
    others = [_airs(ALICE, f"Show {i}", i) for i in range(2, 6)]

    capped = apply_cap({ALICE: [dump, *others]}, 5, today=TODAY)

    assert len(capped[ALICE]) == 5
    assert all(c.kind == "airs_today" for c in capped[ALICE])


def test_the_cap_is_per_user():
    capped = apply_cap(
        {
            ALICE: [_airs(ALICE, f"Show {i}", i) for i in range(3)],
            BOB: [_airs(BOB, f"Show {i}", 10 + i) for i in range(3)],
        },
        2,
        today=TODAY,
    )

    assert [c.kind for c in capped[ALICE]] == ["airs_today", "airs_today", "summary"]
    assert capped[BOB][-1].key == f"summary:{BOB}:2026-09-26"
    assert capped[BOB][-1].count == 1


def test_group_by_user_keeps_input_order():
    a1, b1, a2 = _airs(ALICE, "A", 1), _airs(BOB, "B", 2), _airs(ALICE, "C", 3)

    assert group_by_user([a1, b1, a2]) == {ALICE: [a1, a2], BOB: [b1]}
