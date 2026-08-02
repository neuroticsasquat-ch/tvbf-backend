"""Regression tests for specials — episodes TV Maze sends with a null `number`.

`embed[]=episodes` silently omits these; only `/shows/{id}/episodes?specials=1`
returns them (NEU-933). The payloads below are verbatim from
`GET https://api.tvmaze.com/shows/168/episodes?specials=1`, whose one special is
`Buy Hard: The Jeff and Lester Story` — the case that first surfaced the gap.
"""

from tvbf.tvmaze.api_payloads import TVMazeEpisode

# Verbatim from the live endpoint, trimmed to the fields we parse.
LIVE_SPECIAL = {
    "id": 153062,
    "name": "Buy Hard: The Jeff and Lester Story",
    "season": 4,
    "number": None,
    "airdate": "",
    "airtime": "",
    "runtime": None,
    "summary": None,
    "image": None,
    "rating": {"average": None},
}


def test_special_parses_with_a_null_number():
    ep = TVMazeEpisode.model_validate(LIVE_SPECIAL)
    assert ep.id == 153062
    assert ep.number is None
    assert ep.season == 4


def test_special_coerces_empty_airdate_and_airtime_to_none():
    """Specials carry `""` for unknown air fields, same as any other episode."""
    ep = TVMazeEpisode.model_validate(LIVE_SPECIAL)
    assert ep.airdate is None
    assert ep.airtime is None


def test_a_numbered_episode_still_keeps_its_number():
    ep = TVMazeEpisode.model_validate({**LIVE_SPECIAL, "id": 12, "number": 1})
    assert ep.number == 1
