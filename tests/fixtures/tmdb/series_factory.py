"""Realistic TMDB payloads, as dicts.

**Every key here is a key the live API actually sends**, spelled the way it
sends it — measured 2026-08-09 against `/tv/1396` with the audit's 11-namespace
append list. That matters more than usual: `tmdb/api_payloads.py` binds its
fields by alias alone, precisely so a fixture cannot invent a friendlier key and
pass while a real payload parses to `None`. Building test input from anything
other than the real shape would defeat that.

Two measured quirks are baked in and should not be "fixed":

- an appended `season/N` block carries **no `id`** (only `_id`, which is
  TMDB-internal), so seasons take their identity from `seasons[]`;
- `episode_run_time` is usually `[]`, which is why the show's scalar `runtime`
  is derived from episode runtimes instead.
"""

from typing import Any


def make_episode(
    tmdb_id: int, season_number: int, episode_number: int, **overrides: Any
) -> dict[str, Any]:
    return {
        "id": tmdb_id,
        "season_number": season_number,
        "episode_number": episode_number,
        "name": f"S{season_number}E{episode_number}",
        "overview": "",
        "air_date": "2008-01-20",
        "episode_type": "standard",
        "production_code": "",
        "runtime": 45,
        "show_id": 1396,
        "still_path": "/still.jpg",
        "vote_average": 8.1,
        "vote_count": 100,
        # NEU-1038's, and unparsed here on purpose — present so the fixture
        # matches the payload a real fetch hands to the parser.
        "crew": [],
        "guest_stars": [],
    } | overrides


def make_season_summary(tmdb_id: int, season_number: int, **overrides: Any) -> dict[str, Any]:
    """An entry of the series payload's `seasons[]` — the only place a season's
    `id` and its `season_number` appear together."""
    return {
        "id": tmdb_id,
        "season_number": season_number,
        "name": f"Season {season_number}",
        "overview": "",
        "poster_path": "/poster.jpg",
        "air_date": "2008-01-20",
        "vote_average": 8.3,
        "episode_count": 7,
    } | overrides


def make_season_detail(
    season_number: int, episodes: list[dict[str, Any]] | None = None, **overrides: Any
) -> dict[str, Any]:
    """An appended `season/N` block. Carries `_id` and **not** `id`."""
    return {
        "_id": f"52e8fd2d760ee3466174{season_number:04d}",
        "air_date": "2008-01-20",
        "name": f"Season {season_number}",
        "overview": "",
        "poster_path": "/poster.jpg",
        "season_number": season_number,
        "vote_average": 8.3,
        "networks": [],
        "episodes": episodes if episodes is not None else [],
    } | overrides


def make_series(
    tmdb_id: int = 1396,
    *,
    seasons: int = 1,
    episodes_per_season: int = 2,
    append_seasons: bool = True,
    **overrides: Any,
) -> dict[str, Any]:
    """A `/tv/{id}` response body.

    Only the series body — no appended namespaces. Tests that want one add it
    explicitly, because "namespace absent" and "namespace empty" are different
    instructions to the writers and a factory that guessed would hide that.
    """
    season_numbers = list(range(1, seasons + 1))
    payload: dict[str, Any] = {
        "id": tmdb_id,
        "name": f"Show {tmdb_id}",
        "original_name": f"Show {tmdb_id}",
        "overview": "A show.",
        "tagline": "",
        "homepage": "",
        "type": "Scripted",
        "adult": False,
        "status": "Ended",
        "in_production": False,
        "first_air_date": "2008-01-20",
        "last_air_date": "2013-09-29",
        "original_language": "en",
        "languages": ["en"],
        "spoken_languages": [{"english_name": "English", "iso_639_1": "en", "name": "English"}],
        "origin_country": ["US"],
        "production_countries": [{"iso_3166_1": "US", "name": "United States of America"}],
        "popularity": 123.4,
        "vote_average": 8.9,
        "vote_count": 15000,
        "poster_path": "/poster.jpg",
        "backdrop_path": "/backdrop.jpg",
        "number_of_episodes": seasons * episodes_per_season,
        "number_of_seasons": seasons,
        "episode_run_time": [],
        "genres": [{"id": 18, "name": "Drama"}],
        "networks": [{"id": 174, "logo_path": "/amc.png", "name": "AMC", "origin_country": "US"}],
        "production_companies": [
            {"id": 11073, "logo_path": "/sony.png", "name": "Sony", "origin_country": "US"}
        ],
        "created_by": [],
        "seasons": [
            make_season_summary(tmdb_id * 100 + n, n, episode_count=episodes_per_season)
            for n in season_numbers
        ],
        "last_episode_to_air": None,
        "next_episode_to_air": None,
    }
    if append_seasons:
        for n in season_numbers:
            payload[f"season/{n}"] = make_season_detail(
                n,
                [
                    make_episode(tmdb_id * 10000 + n * 100 + e, n, e)
                    for e in range(1, episodes_per_season + 1)
                ],
            )
    return payload | overrides
