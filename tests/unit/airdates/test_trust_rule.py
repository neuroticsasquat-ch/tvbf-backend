"""The trust rule: when an offset is written and when the job refuses (NEU-1145 §4.5).

Every case here is a case where guessing would have been cheap and wrong. The
asymmetry is the design: a false offset silently re-labels a user's watch
history, while a refusal is one line in a log a human already reads.
"""

from datetime import date

from tvbf.airdates.api_payloads import TVMazeEpisode
from tvbf.airdates.reconcile import _oracle_episodes, _unambiguous, judge_seasons


def _ours(*entries: tuple[int, int, str]) -> dict[tuple[int, int], date]:
    return {(s, e): date.fromisoformat(d) for s, e, d in entries}


def test_a_unanimous_one_day_shift_is_written():
    verdicts = judge_seasons(
        _ours((1, 1, "2023-05-04"), (1, 2, "2023-05-11"), (1, 3, "2023-05-18")),
        _ours((1, 1, "2023-05-05"), (1, 2, "2023-05-12"), (1, 3, "2023-05-19")),
    )
    assert [(v.season_number, v.offset_days, v.episodes_compared) for v in verdicts] == [(1, 1, 3)]


def test_agreement_is_a_verdict_of_zero_not_a_refusal():
    """Slow Horses agrees on all five seasons. Zero is what retracts an offset
    a previous run wrote, so it must be a decision rather than an absence."""
    verdicts = judge_seasons(
        _ours((1, 1, "2022-04-01"), (1, 2, "2022-04-08")),
        _ours((1, 1, "2022-04-01"), (1, 2, "2022-04-08")),
    )
    assert verdicts[0].offset_days == 0
    assert verdicts[0].reason is None


def test_one_agreeing_episode_is_not_enough():
    """A single pair cannot tell a season's convention from a one-off upstream
    typo, and every off-by-one in the data would satisfy a rule of one."""
    verdicts = judge_seasons(_ours((1, 1, "2023-05-04")), _ours((1, 1, "2023-05-05")))
    assert verdicts[0].offset_days is None
    assert verdicts[0].reason == "too_few"


def test_a_season_that_does_not_agree_with_itself_is_refused():
    verdicts = judge_seasons(
        _ours((1, 1, "2023-05-04"), (1, 2, "2023-05-11"), (1, 3, "2023-05-18")),
        _ours((1, 1, "2023-05-05"), (1, 2, "2023-05-11"), (1, 3, "2023-05-19")),
    )
    assert verdicts[0].offset_days is None
    assert verdicts[0].reason == "inconsistent"


def test_a_larger_unanimous_shift_is_refused_not_applied():
    """The clamp is principled: a timezone artifact can only ever produce ±1.
    Any other delta is a different disagreement, and following it would make the
    mirror track the oracle's schedule rather than fix TMDB's coast."""
    verdicts = judge_seasons(
        _ours((2, 1, "2024-01-01"), (2, 2, "2024-01-08")),
        _ours((2, 1, "2024-01-08"), (2, 2, "2024-01-15")),
    )
    assert verdicts[0].offset_days is None
    assert verdicts[0].reason == "out_of_range"
    assert verdicts[0].deltas == (7,)


def test_shrinking_season_three_is_refused_by_both_clauses_independently():
    """The worked example. TMDB spreads a two-episode premiere across two weeks
    where the oracle dates both on the same day, so the per-episode deltas run
    `+1, +6, +6`. A job trusting the oracle's delta would have moved nine
    episodes by six days; the cost of refusing is that E1 stays a day early,
    which is a better artifact than a season that contradicts itself."""
    verdicts = judge_seasons(
        _ours((3, 1, "2026-01-27"), (3, 2, "2026-02-03"), (3, 3, "2026-02-10")),
        _ours((3, 1, "2026-01-28"), (3, 2, "2026-01-28"), (3, 3, "2026-02-04")),
    )
    assert verdicts[0].offset_days is None
    assert verdicts[0].reason == "inconsistent"
    assert set(verdicts[0].deltas) == {1, -6}


def test_each_season_is_judged_on_its_own_evidence():
    """Ted Lasso again, at the grain the offset is actually keyed at."""
    verdicts = judge_seasons(
        _ours(
            (1, 1, "2020-08-14"), (1, 2, "2020-08-21"), (3, 1, "2023-03-14"), (3, 2, "2023-03-21")
        ),
        _ours(
            (1, 1, "2020-08-14"), (1, 2, "2020-08-21"), (3, 1, "2023-03-15"), (3, 2, "2023-03-22")
        ),
    )
    assert {v.season_number: v.offset_days for v in verdicts} == {1: 0, 3: 1}


def test_a_season_the_oracle_cannot_speak_to_is_reported_rather_than_omitted():
    """Most often season 0, whose specials neither side can key. It is a verdict
    of its own rather than an omission so nothing about the pass is silent, and
    it is separated from a refusal because there was no evidence to reject."""
    verdicts = judge_seasons(_ours((1, 1, "2024-01-01")), _ours((2, 1, "2025-01-01")))
    assert [(v.season_number, v.reason, v.episodes_compared) for v in verdicts] == [
        (1, "no_overlap", 0)
    ]


def test_a_season_the_oracle_only_partly_covers_still_needs_two_episodes():
    verdicts = judge_seasons(
        _ours((1, 1, "2024-01-01"), (1, 2, "2024-01-08")), _ours((1, 1, "2024-01-02"))
    )
    assert verdicts[0].reason == "too_few"


def test_a_duplicated_key_is_dropped_from_both_sides():
    """Uniqueness is required on both sides. The copy left 2,298 duplicate
    `(show, season, number)` triples in the mirror and 13 shows number two
    seasons the same; a duplicated key cannot say which date it means, and
    picking one would be the pass adjudicating a question nobody asked."""
    kept = _unambiguous(
        [((1, 1), date(2024, 1, 1)), ((1, 1), date(2024, 1, 2)), ((1, 2), date(2024, 1, 8))]
    )
    assert kept == {(1, 2): date(2024, 1, 8)}


def test_oracle_specials_and_undated_episodes_drop_out():
    """TV Maze numbers a special `null` and leaves an unscheduled episode's
    `airdate` empty. Neither can be paired, so neither reaches a verdict — and
    the `""` is already `None` by here, because `OptionalDate` coerced it at the
    parser boundary rather than at the point of use."""
    parsed = _oracle_episodes(
        [
            TVMazeEpisode.model_validate(e)
            for e in [
                {"season": 1, "number": 1, "airdate": "2024-01-01"},
                {"season": 1, "number": None, "airdate": "2024-01-02"},
                {"season": 1, "number": 2, "airdate": ""},
                {"season": 1, "number": 3, "airdate": None},
            ]
        ]
    )
    assert parsed == {(1, 1): date(2024, 1, 1)}


def test_a_copied_specials_negative_number_never_keys_anything():
    """The migration numbers a copied TV Maze special -1, -2, … within its
    season (`catalog/episodes.py`). Those are unmappable by construction, and
    coercing them into a key would pair them with real episode 1."""
    special = TVMazeEpisode(season=0, number=-1, airdate=date(2024, 1, 1))
    assert _oracle_episodes([special]) == {}
