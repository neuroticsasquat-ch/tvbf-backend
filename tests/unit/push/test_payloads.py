import json
from datetime import date
from uuid import UUID

import pytest

from tvbf.push.candidates import Candidate
from tvbf.push.payloads import MAX_TEXT_CHARS, build_payload

USER = UUID("00000000-0000-0000-0000-000000000001")


def _airs_today(**overrides) -> Candidate:
    fields = {
        "user_id": USER,
        "kind": "airs_today",
        "key": "airs_today:12345:2026-09-26",
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
        "key": "airs_today:12345:2026-09-26",
        "kind": "airs_today",
        "title": "Severance",
        "body": "S2E4 “Woe’s Hollow” airs today",
        "url": "/episodes/12345",
        "icon": "https://image.tmdb.org/t/p/w185/abc.jpg",
    }


@pytest.mark.parametrize("name", [None, ""])
def test_airs_today_without_an_episode_title(name):
    assert build_payload(_airs_today(episode_name=name))["body"] == "S2E4 airs today"


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


def test_summary():
    payload = build_payload(
        Candidate(user_id=USER, kind="summary", key=f"summary:{USER}:2026-09-26", count=3)
    )

    assert payload == {
        "key": f"summary:{USER}:2026-09-26",
        "kind": "summary",
        "title": "TV BingeFriend",
        "body": "3 more updates today",
        "url": "/upcoming",
    }


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
