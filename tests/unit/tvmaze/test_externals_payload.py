"""Regression tests for the `externals` block's upstream key names.

TV Maze returns the TVDB id under `thetvdb`, not `tvdb`. A field declared
without that alias parses to None silently — nothing errors, the column just
stays null — so these tests pin the key names against a verbatim copy of the
live payload from `GET https://api.tvmaze.com/shows/1`.
"""

import pytest

from tvbf.tvmaze.api_payloads import TVMazeExternals, TVMazeShow

# Verbatim from `curl -s https://api.tvmaze.com/shows/1 | jq .externals`.
LIVE_EXTERNALS = {"tvrage": 25988, "thetvdb": 264492, "imdb": "tt1553656"}


def _show_payload(**overrides):
    base = {
        "id": 1,
        "name": "Under the Dome",
        "updated": 1,
        "network": None,
        "webChannel": None,
        "genres": [],
        "_embedded": {"episodes": [], "seasons": []},
    }
    base.update(overrides)
    return base


def test_live_externals_payload_populates_every_field():
    externals = TVMazeExternals.model_validate(LIVE_EXTERNALS)
    assert externals.imdb == "tt1553656"
    assert externals.tvdb == 264492
    assert externals.tvrage == 25988


def test_show_externals_populates_tvdb_from_thetvdb_key():
    show = TVMazeShow.model_validate(_show_payload(externals=LIVE_EXTERNALS))
    assert show.externals is not None
    assert show.externals.tvdb == 264492


@pytest.mark.parametrize(
    ("wire_key", "field_name"),
    [("imdb", "imdb"), ("thetvdb", "tvdb"), ("tvrage", "tvrage")],
)
def test_externals_fields_are_individually_optional(wire_key, field_name):
    payload = {k: v for k, v in LIVE_EXTERNALS.items() if k != wire_key}
    externals = TVMazeExternals.model_validate(payload)
    assert getattr(externals, field_name) is None


def test_field_name_tvdb_is_not_an_accepted_key():
    """`tvdb` is not a key TV Maze ever sends — accepting it would let a
    fabricated fixture pass while real payloads parse to None."""
    externals = TVMazeExternals.model_validate({"tvdb": 264492})
    assert externals.tvdb is None
