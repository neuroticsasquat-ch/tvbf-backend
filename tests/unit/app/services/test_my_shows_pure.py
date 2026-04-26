"""Unit tests for the pure (no-DB) pieces of my_shows_service.

These tests construct DTOs in memory and call the service's pure helpers
directly. They run in milliseconds and exercise every sort branch + the
ref-resolution logic in build_show_summary_from_refs.
"""

from datetime import UTC, date, datetime
from types import SimpleNamespace

from tvbf.app.dto import MyShowEntry, UpcomingEntry, WatchNextEntry
from tvbf.app.services.my_shows_service import (
    build_show_summary_from_refs,
    sort_my_shows,
    sort_upcoming,
    sort_watch_next,
)
from tvbf.tvmaze.dto import EpisodeOut, ShowSummary

# ---------------------------------------------------------------------------
# Fixtures (factories — pure construction, no fixtures decorator)
# ---------------------------------------------------------------------------


def _show_summary(*, id: int, name: str) -> ShowSummary:
    return ShowSummary(
        id=id,
        name=name,
        type=None,
        status=None,
        language=None,
        premiered=None,
        ended=None,
        image_medium=None,
        image_original=None,
        network=None,
        web_channel=None,
        genres=[],
    )


def _episode_out(
    *,
    id: int,
    show_id: int,
    season: int = 1,
    number: int = 1,
    airdate: date | None = None,
    name: str | None = None,
) -> EpisodeOut:
    return EpisodeOut(
        id=id,
        show_id=show_id,
        season_id=None,
        season=season,
        number=number,
        name=name,
        airdate=airdate,
        airtime=None,
        runtime=None,
        summary=None,
        image_medium=None,
        image_original=None,
    )


def _my_show(
    *,
    id: int,
    name: str,
    watched: int = 0,
    total: int = 0,
    next_ep: EpisodeOut | None = None,
    added_at: datetime | None = None,
) -> MyShowEntry:
    return MyShowEntry(
        show=_show_summary(id=id, name=name),
        watched_episode_count=watched,
        total_episode_count=total,
        next_episode=next_ep,
        added_at=added_at or datetime(2026, 1, 1, tzinfo=UTC),
    )


def _watch_next(
    *, id: int, name: str, airdate: date | None = None, ep_number: int = 1
) -> WatchNextEntry:
    return WatchNextEntry(
        show=_show_summary(id=id, name=name),
        episode=_episode_out(id=id * 100, show_id=id, number=ep_number, airdate=airdate),
    )


def _upcoming(*, id: int, name: str, airdate: date | None = None) -> UpcomingEntry:
    return UpcomingEntry(
        show=_show_summary(id=id, name=name),
        episode=_episode_out(id=id * 100, show_id=id, airdate=airdate),
    )


# ---------------------------------------------------------------------------
# sort_my_shows
# ---------------------------------------------------------------------------


def test_sort_my_shows_name_asc_is_case_insensitive():
    entries = [
        _my_show(id=1, name="charlie"),
        _my_show(id=2, name="Alpha"),
        _my_show(id=3, name="bravo"),
    ]
    out = sort_my_shows(entries, "name_asc", latest_aired={})
    assert [e.show.name for e in out] == ["Alpha", "bravo", "charlie"]


def test_sort_my_shows_name_desc():
    entries = [_my_show(id=1, name="alpha"), _my_show(id=2, name="bravo")]
    out = sort_my_shows(entries, "name_desc", latest_aired={})
    assert [e.show.name for e in out] == ["bravo", "alpha"]


def test_sort_my_shows_added_orders_most_recent_first():
    entries = [
        _my_show(id=1, name="A", added_at=datetime(2026, 1, 5, tzinfo=UTC)),
        _my_show(id=2, name="B", added_at=datetime(2026, 1, 10, tzinfo=UTC)),
        _my_show(id=3, name="C", added_at=datetime(2026, 1, 1, tzinfo=UTC)),
    ]
    out = sort_my_shows(entries, "added", latest_aired={})
    assert [e.show.id for e in out] == [2, 1, 3]


def test_sort_my_shows_recent_activity_uses_latest_aired_then_name():
    entries = [
        _my_show(id=1, name="A"),
        _my_show(id=2, name="B"),
        _my_show(id=3, name="C"),
    ]
    latest = {1: date(2026, 4, 1), 2: date(2026, 4, 10), 3: date(2026, 4, 10)}
    # Show 2 and 3 tie on date; secondary sort = name asc (so "B" before "C" — but
    # since the whole sort is reverse=True, the within-tie order is name desc).
    out = sort_my_shows(entries, "recent_activity", latest_aired=latest)
    assert [e.show.id for e in out] == [3, 2, 1]


def test_sort_my_shows_recent_activity_falls_back_to_epoch_when_show_has_no_aired():
    entries = [_my_show(id=1, name="HasAired"), _my_show(id=2, name="NoAired")]
    latest = {1: date(2026, 4, 1)}  # show 2 missing
    out = sort_my_shows(entries, "recent_activity", latest_aired=latest)
    assert [e.show.id for e in out] == [1, 2]


# ---------------------------------------------------------------------------
# sort_watch_next
# ---------------------------------------------------------------------------


def test_sort_watch_next_default_is_airdate_desc():
    entries = [
        _watch_next(id=1, name="Old", airdate=date(2026, 1, 1)),
        _watch_next(id=2, name="New", airdate=date(2026, 4, 1)),
    ]
    out = sort_watch_next(entries, "airdate_desc")
    assert [e.show.id for e in out] == [2, 1]


def test_sort_watch_next_airdate_asc():
    entries = [
        _watch_next(id=1, name="Old", airdate=date(2026, 1, 1)),
        _watch_next(id=2, name="New", airdate=date(2026, 4, 1)),
    ]
    out = sort_watch_next(entries, "airdate_asc")
    assert [e.show.id for e in out] == [1, 2]


def test_sort_watch_next_name_modes():
    entries = [_watch_next(id=1, name="bravo"), _watch_next(id=2, name="alpha")]
    asc = sort_watch_next(entries, "name_asc")
    assert [e.show.id for e in asc] == [2, 1]
    desc = sort_watch_next(entries, "name_desc")
    assert [e.show.id for e in desc] == [1, 2]


def test_sort_watch_next_null_airdates_fall_to_bottom_under_desc():
    # date.min as fallback — under reverse=True, that's the bottom.
    entries = [
        _watch_next(id=1, name="HasDate", airdate=date(2026, 4, 1)),
        _watch_next(id=2, name="NoDate", airdate=None),
    ]
    out = sort_watch_next(entries, "airdate_desc")
    assert [e.show.id for e in out] == [1, 2]


def test_sort_watch_next_secondary_sort_by_name_when_dates_tie():
    same = date(2026, 4, 1)
    entries = [
        _watch_next(id=1, name="bravo", airdate=same),
        _watch_next(id=2, name="alpha", airdate=same),
    ]
    out = sort_watch_next(entries, "airdate_asc")
    # Under airdate_asc: same date ⇒ name asc.
    assert [e.show.id for e in out] == [2, 1]


# ---------------------------------------------------------------------------
# sort_upcoming
# ---------------------------------------------------------------------------


def test_sort_upcoming_default_is_airdate_asc():
    entries = [
        _upcoming(id=1, name="Far", airdate=date(2026, 6, 1)),
        _upcoming(id=2, name="Near", airdate=date(2026, 5, 1)),
    ]
    out = sort_upcoming(entries, "airdate_asc")
    assert [e.show.id for e in out] == [2, 1]


def test_sort_upcoming_airdate_desc():
    entries = [
        _upcoming(id=1, name="Far", airdate=date(2026, 6, 1)),
        _upcoming(id=2, name="Near", airdate=date(2026, 5, 1)),
    ]
    out = sort_upcoming(entries, "airdate_desc")
    assert [e.show.id for e in out] == [1, 2]


def test_sort_upcoming_name_modes():
    entries = [_upcoming(id=1, name="bravo"), _upcoming(id=2, name="alpha")]
    asc = sort_upcoming(entries, "name_asc")
    assert [e.show.id for e in asc] == [2, 1]
    desc = sort_upcoming(entries, "name_desc")
    assert [e.show.id for e in desc] == [1, 2]


# ---------------------------------------------------------------------------
# build_show_summary_from_refs
# ---------------------------------------------------------------------------


def _show_model(
    *,
    id: int = 1,
    name: str = "Show",
    network_id: int | None = None,
    web_channel_id: int | None = None,
):
    """Light stand-in for the SQLAlchemy Show model — only the fields
    build_show_summary touches."""
    return SimpleNamespace(
        id=id,
        name=name,
        type=None,
        language=None,
        status=None,
        premiered=None,
        ended=None,
        image_medium=None,
        image_original=None,
        network_id=network_id,
        web_channel_id=web_channel_id,
    )


def test_build_show_summary_with_no_refs():
    show = _show_model(id=1, name="X")
    out = build_show_summary_from_refs(
        show,  # type: ignore[arg-type]
        genres_by_show={},
        networks_by_id={},
        wcs_by_id={},
    )
    assert out.id == 1
    assert out.name == "X"
    assert out.genres == []
    assert out.network is None
    assert out.web_channel is None


def test_build_show_summary_with_genres():
    show = _show_model(id=1, name="X")
    out = build_show_summary_from_refs(
        show,  # type: ignore[arg-type]
        genres_by_show={1: ["Drama", "Sci-Fi"]},
        networks_by_id={},
        wcs_by_id={},
    )
    assert out.genres == ["Drama", "Sci-Fi"]


def test_build_show_summary_resolves_network_when_show_has_network_id():
    show = _show_model(id=1, name="X", network_id=5)
    network = SimpleNamespace(id=5, name="HBO")
    out = build_show_summary_from_refs(
        show,  # type: ignore[arg-type]
        genres_by_show={},
        networks_by_id={5: network},
        wcs_by_id={},
    )
    assert out.network is not None
    assert out.network.id == 5
    assert out.network.name == "HBO"
    assert out.web_channel is None


def test_build_show_summary_resolves_web_channel_when_show_has_web_channel_id():
    show = _show_model(id=1, name="X", web_channel_id=42)
    wc = SimpleNamespace(id=42, name="Netflix")
    out = build_show_summary_from_refs(
        show,  # type: ignore[arg-type]
        genres_by_show={},
        networks_by_id={},
        wcs_by_id={42: wc},
    )
    assert out.network is None
    assert out.web_channel is not None
    assert out.web_channel.id == 42
    assert out.web_channel.name == "Netflix"


def test_build_show_summary_skips_network_when_id_present_but_not_in_map():
    """Defensive: if the lookup map is missing an entry, the summary degrades to
    None rather than crashing. Mirrors hydrate_show_refs's contract."""
    show = _show_model(id=1, name="X", network_id=5)
    out = build_show_summary_from_refs(
        show,  # type: ignore[arg-type]
        genres_by_show={},
        networks_by_id={},
        wcs_by_id={},
    )
    assert out.network is None


def test_build_show_summary_with_both_network_and_web_channel():
    show = _show_model(id=1, name="X", network_id=5, web_channel_id=42)
    out = build_show_summary_from_refs(
        show,  # type: ignore[arg-type]
        genres_by_show={1: ["Drama"]},
        networks_by_id={5: SimpleNamespace(id=5, name="HBO")},
        wcs_by_id={42: SimpleNamespace(id=42, name="Netflix")},
    )
    assert out.network is not None and out.network.name == "HBO"
    assert out.web_channel is not None and out.web_channel.name == "Netflix"
    assert out.genres == ["Drama"]
