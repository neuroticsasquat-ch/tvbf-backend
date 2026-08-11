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


def make_role(
    character: str, episode_count: int, credit_id: str | None = None, **overrides: Any
) -> dict[str, Any]:
    """An entry of a cast member's `roles[]` — the nesting that makes
    `episode_count` a per-character measure."""
    return {
        "credit_id": credit_id if credit_id is not None else f"role-{character}",
        "character": character,
        "episode_count": episode_count,
    } | overrides


def make_job(job: str, episode_count: int, credit_id: str | None = None) -> dict[str, Any]:
    """An entry of a crew member's `jobs[]`. Carries no `department` — that sits
    on the crew entry above it."""
    return {
        "credit_id": credit_id if credit_id is not None else f"job-{job}",
        "job": job,
        "episode_count": episode_count,
    }


def _credit_person(tmdb_person_id: int, name: str) -> dict[str, Any]:
    return {
        "adult": False,
        "gender": 2,
        "id": tmdb_person_id,
        "known_for_department": "Acting",
        "name": name,
        "original_name": name,
        "popularity": 12.3,
        "profile_path": "/profile.jpg",
    }


def make_cast_member(
    tmdb_person_id: int,
    name: str,
    roles: list[dict[str, Any]],
    *,
    order: int = 0,
    **overrides: Any,
) -> dict[str, Any]:
    """An entry of `aggregate_credits.cast`. Carries `order`, unlike crew."""
    return (
        _credit_person(tmdb_person_id, name)
        | {
            "roles": roles,
            # Upstream states this itself; summing the nested counts is the closest
            # a fixture can get without inventing a number. `None` is skipped
            # rather than coerced, because a role of unknown size adds nothing
            # knowable to the entry total.
            "total_episode_count": sum(r["episode_count"] or 0 for r in roles),
            "order": order,
        }
        | overrides
    )


def make_crew_member(
    tmdb_person_id: int,
    name: str,
    department: str,
    jobs: list[dict[str, Any]],
    **overrides: Any,
) -> dict[str, Any]:
    """An entry of `aggregate_credits.crew`. Carries **no** `order` — measured
    absent on all 2,066 sampled show-crew entries."""
    return (
        _credit_person(tmdb_person_id, name)
        | {
            "department": department,
            "jobs": jobs,
            "total_episode_count": sum(j["episode_count"] or 0 for j in jobs),
        }
        | overrides
    )


def make_aggregate_credits(
    cast: list[dict[str, Any]] | None = None,
    crew: list[dict[str, Any]] | None = None,
    tmdb_id: int = 1396,
) -> dict[str, Any]:
    """The `aggregate_credits` namespace. `id` is the *show's* id, which the
    writer ignores because its caller already has it."""
    return {"id": tmdb_id, "cast": cast or [], "crew": crew or []}


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
