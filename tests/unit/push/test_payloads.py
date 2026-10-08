import json
from datetime import date
from uuid import UUID

import pytest

from tvbf.push.candidates import AiredEpisode, Candidate, DeliveryTaskLabel
from tvbf.push.payloads import MAX_TEXT_CHARS, build_payload

USER = UUID("00000000-0000-0000-0000-000000000001")


def _airs_today(**overrides) -> Candidate:
    fields = {
        "user_id": USER,
        "kind": "airs_today",
        "key": "airs_today:7:2026-09-26",
        "show_id": 7,
        "episode_id": 12345,
        "show_name": "Severance",
        "poster_path": "/abc.jpg",
        "season_number": 2,
        "episode_number": 4,
        "episode_name": "Woe’s Hollow",
        "air_date": date(2026, 9, 26),
    }
    return Candidate(**(fields | overrides))


def _episodes(*codes: tuple[int, int], name: str | None = None) -> tuple[AiredEpisode, ...]:
    return tuple(
        AiredEpisode(id=1000 + i, season_number=s, episode_number=e, name=name)
        for i, (s, e) in enumerate(codes)
    )


def _group(*codes: tuple[int, int], **overrides) -> Candidate:
    """An airs-today candidate folding `codes`, its single fields the first's."""
    episodes = _episodes(*codes, name="Episode title")
    return _airs_today(
        episode_id=episodes[0].id,
        season_number=episodes[0].season_number,
        episode_number=episodes[0].episode_number,
        episode_name=episodes[0].name,
        episodes=episodes,
        **overrides,
    )


def _event(kind, **overrides) -> Candidate:
    fields = {
        "user_id": USER,
        "kind": kind,
        "key": f"{kind}:99",
        "show_id": 7,
        "event_id": 99,
        "show_name": "Severance",
        "poster_path": None,
    }
    return Candidate(**(fields | overrides))


def test_airs_today_matches_the_spec_example():
    assert build_payload(_airs_today()) == {
        "key": "airs_today:7:2026-09-26",
        "kind": "airs_today",
        "title": "Severance",
        "body": "S2E4 “Woe’s Hollow” airs today",
        "url": "/episodes/12345",
        "icon": "https://image.tmdb.org/t/p/w185/abc.jpg",
    }


@pytest.mark.parametrize("name", [None, ""])
def test_airs_today_without_an_episode_title(name):
    assert build_payload(_airs_today(episode_name=name))["body"] == "S2E4 airs today"


# --- one push per show (NEU-1539) ---------------------------------------------


def test_a_group_of_one_renders_as_the_single_episode():
    payload = build_payload(_group((2, 4)))

    assert payload["body"] == "S2E4 “Episode title” airs today"
    assert payload["url"] == "/episodes/1000"


def test_a_season_dump_is_one_push_with_a_range():
    payload = build_payload(_group(*((2, n) for n in range(1, 9))))

    assert payload["body"] == "8 episodes air today (S2E1–E8)"
    assert payload["url"] == "/shows/7/episodes?season=2"
    assert payload["title"] == "Severance"
    assert payload["key"] == "airs_today:7:2026-09-26"


def test_two_episodes_are_a_range_too():
    assert build_payload(_group((3, 7), (3, 8)))["body"] == "2 episodes air today (S3E7–E8)"


def test_a_broken_run_lists_the_codes():
    assert build_payload(_group((2, 1), (2, 3), (2, 4)))["body"] == (
        "3 episodes air today (S2E1, S2E3, S2E4)"
    )


def test_episodes_across_seasons_list_the_codes_and_link_the_first_season():
    payload = build_payload(_group((1, 10), (2, 1)))

    assert payload["body"] == "2 episodes air today (S1E10, S2E1)"
    assert payload["url"] == "/shows/7/episodes?season=1"


def test_a_long_broken_list_keeps_whole_codes_and_counts_the_rest():
    body = build_payload(_group(*((1, n) for n in range(1, 200, 2))))["body"]

    assert len(body) <= MAX_TEXT_CHARS
    assert body.startswith("100 episodes air today (S1E1, S1E3, ")
    codes, more = body[len("100 episodes air today (") : -1].rsplit(" and ", 1)
    shown = codes.split(", ")
    assert more == f"{100 - len(shown)} more"
    assert all(code.startswith("S1E") for code in shown)


def test_a_grouped_body_ignores_episode_titles():
    body = build_payload(_group((2, 1), (2, 2)))["body"]

    assert "Episode title" not in body


def test_the_worst_case_group_stays_under_4kb():
    payload = build_payload(
        _group(
            *((s, e) for s in range(1, 31) for e in range(1, 11)),
            show_name="📺" * 1000,
            poster_path="/" + "a" * 64,
        )
    )

    assert len(json.dumps(payload).encode()) < 4096


def test_premiere_set_renders_the_corrected_date():
    payload = build_payload(
        _event("premiere_set", season_id=5, season_number=3, air_date=date(2026, 10, 5))
    )

    assert payload["body"] == "Season 3 premieres Oct 5"
    assert payload["url"] == "/shows/7"
    assert payload["title"] == "Severance"


def test_premiere_moved_renders_the_corrected_date():
    payload = build_payload(
        _event("premiere_moved", season_id=5, season_number=3, air_date=date(2026, 11, 12))
    )

    assert payload["body"] == "Season 3 moved to Nov 12"


def test_a_premiere_without_a_date_is_a_caller_bug():
    with pytest.raises(ValueError):
        build_payload(_event("premiere_set", season_number=3, air_date=None))


@pytest.mark.parametrize(
    ("status", "body"), [("Ended", "Marked as ended"), ("Canceled", "Marked as cancelled")]
)
def test_ended_reads_the_raw_status(status, body):
    assert build_payload(_event("ended", status=status))["body"] == body


def test_revived():
    assert build_payload(_event("revived", status="Returning Series"))["body"] == (
        "Renewed — more episodes are coming"
    )


def _summary(count: int, *names: str, task: DeliveryTaskLabel = "events") -> Candidate:
    return Candidate(
        user_id=USER,
        kind="summary",
        key=f"summary:{task}:{USER}:2026-09-26",
        count=count,
        show_names=names,
        task=task,
    )


def test_the_events_summary():
    payload = build_payload(_summary(3, "Andor", "Severance"))

    assert payload == {
        "key": f"summary:events:{USER}:2026-09-26",
        "kind": "summary",
        "title": "3 more updates today",
        "body": "Andor, Severance",
        "url": "/upcoming",
    }


def test_an_events_summary_of_one_update_is_singular():
    assert build_payload(_summary(1, "Andor"))["title"] == "1 more update today"


def test_the_airs_today_summary():
    """Titled by task (NEU-1540): the airs-today overflow is shows, not updates."""
    payload = build_payload(_summary(2, "Andor", "Severance", task="airs_today"))

    assert payload == {
        "key": f"summary:airs_today:{USER}:2026-09-26",
        "kind": "summary",
        "title": "2 more shows air today",
        "body": "Andor, Severance",
        "url": "/upcoming",
    }


def test_an_airs_today_summary_of_one_show_is_singular():
    payload = build_payload(_summary(1, "Andor", task="airs_today"))

    assert payload["title"] == "1 more show airs today"


def test_a_summary_lists_as_many_whole_names_as_fit():
    names = tuple(f"Show number {i:02d}" for i in range(20))

    body = build_payload(_summary(20, *names))["body"]

    assert len(body) <= MAX_TEXT_CHARS
    assert body.startswith("Show number 00, Show number 01, ")
    shown = body.split(" and ")[0].split(", ")
    assert all(name in names for name in shown)
    assert body.endswith(f" and {20 - len(shown)} more shows")


def test_a_summary_names_one_remaining_show_in_the_singular():
    names = ("x" * 60, "y" * 60)

    assert build_payload(_summary(2, *names))["body"] == "x" * 60 + " and 1 more show"


def test_a_summary_clips_a_first_name_too_long_to_fit_alone():
    body = build_payload(_summary(2, "x" * 500, "Andor"))["body"]

    assert len(body) == MAX_TEXT_CHARS
    assert body.endswith("x… and 1 more show")


def test_icon_is_omitted_without_a_poster():
    assert "icon" not in build_payload(_airs_today(poster_path=None))


def test_a_long_episode_title_is_clipped_inside_the_body():
    body = build_payload(_airs_today(episode_name="x" * 500))["body"]

    assert body.startswith("S2E4 “xxx")
    assert body.endswith("…” airs today")
    assert len(body) <= MAX_TEXT_CHARS


def test_the_worst_case_payload_stays_under_4kb():
    # Every free-text character astral-plane: twelve bytes each under
    # `json.dumps`' default escaping, which is how `sender.send` serialises.
    payload = build_payload(
        _airs_today(show_name="📺" * 1000, episode_name="📺" * 1000, poster_path="/" + "a" * 64)
    )

    assert len(json.dumps(payload).encode()) < 4096


def test_the_worst_case_summary_stays_under_4kb():
    payload = build_payload(_summary(99, *("📺" * 1000 for _ in range(50))))

    assert len(json.dumps(payload).encode()) < 4096
