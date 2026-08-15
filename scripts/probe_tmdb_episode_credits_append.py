"""Measure whether episode credits ride the *appended* season block (NEU-1040).

`scripts/probe_tmdb_credit_shapes.py` measured episode `guest_stars` and `crew`
on a **standalone** `GET /tv/{id}/season/{n}`. The ingest does not make that
request for most seasons — it appends `season/N` to the series request, and the
appended form is already known to differ from the standalone one (it carries no
`id`; see `api_payloads.TMDBSeasonDetail`). So the question NEU-1040 turns on is
not "does TMDB return episode credits" but **"does it return them on the request
the ingest is already making"**:

* If yes, episode credits cost a column and TV Maze's 29-hour `credits_backfill`
  (ADR-0003) retires with no replacement pass at all.
* If no, they cost **one request per season** — ~188k requests, a separate
  resumable pass with its own watermark, which is the shape ADR-0003 landed on
  and roughly 7 hours at 7.6 req/s.

That is a difference between "free" and "a multi-hour job", and the ticket asks
for it to be measured before implementation rather than assumed.

Secondary question, same request: **is the appended episode list complete?**
A truncated `episodes[]` would make the credits it carries look complete while
silently dropping the tail, so the two lists are compared episode by episode.

Third: **which credit fields are ever missing or blank?** `catalog.person.name`
is NOT NULL and `catalog.crew_role` is NOT NULL in both columns, so a grain that
omits any of them decides whether the writer skips a row or aborts a show. The
show-level answer (`probe_tmdb_credit_shapes.py` Q3) does not carry over — a
guest star is a different payload from a `roles[]` entry.

Run inside the container, with `TMDB_READ_ACCESS_TOKEN` set:

    docker compose exec tvbf-backend python scripts/probe_tmdb_episode_credits_append.py

Costs 2 requests per probed series.
"""

import asyncio
import sys
from collections import Counter

from tvbf.config import get_settings
from tvbf.tmdb.client import TMDBClient

# The same spread as `probe_tmdb_credit_shapes.py`, so the two measurements are
# comparable: a voice-ensemble animation, a prestige drama, a sketch format with
# no characters, an anime, and a genre drama.
PROBE_SERIES = {
    456: "The Simpsons (animation, voice ensemble)",
    1396: "Breaking Bad (drama)",
    1667: "Saturday Night Live (sketch/variety)",
    95479: "Jujutsu Kaisen (anime)",
    82856: "The Mandalorian (genre drama)",
}

PROBE_SEASON = 1


def _keys(rows: list[dict]) -> set[str]:
    return {key for row in rows for key in row}


async def main() -> int:
    appended_guest_keys: set[str] = set()
    appended_crew_keys: set[str] = set()
    standalone_guest_keys: set[str] = set()
    standalone_crew_keys: set[str] = set()
    totals = {
        "appended_guests": 0,
        "appended_crew": 0,
        "standalone_guests": 0,
        "standalone_crew": 0,
    }
    episode_count_mismatch: list[str] = []
    per_episode_mismatch: list[str] = []
    # Q3 — NOT NULL columns downstream: `person.name`, `crew_role.department`,
    # `crew_role.job`. Plus `character`, which is nullable but decides whether a
    # row interns a character at all.
    missing: Counter = Counter()

    settings = get_settings()
    async with TMDBClient(
        base_url=settings.tmdb_base_url,
        read_access_token=settings.tmdb_read_access_token,
        rate_calls=settings.tmdb_rate_limit_requests,
        rate_window=settings.tmdb_rate_limit_window_seconds,
        retry_max_attempts=settings.tmdb_retry_max_attempts,
    ) as client:
        for series_id, label in PROBE_SERIES.items():
            series = await client.get_tv_series(series_id, append=(f"season/{PROBE_SEASON}",))
            appended = series.get(f"season/{PROBE_SEASON}") or {}
            appended_episodes = appended.get("episodes") or []

            standalone = await client.get_tv_season(series_id, PROBE_SEASON)
            standalone_episodes = standalone.get("episodes") or []

            for episode in appended_episodes:
                guests = episode.get("guest_stars") or []
                crew = episode.get("crew") or []
                appended_guest_keys |= _keys(guests)
                appended_crew_keys |= _keys(crew)
                totals["appended_guests"] += len(guests)
                totals["appended_crew"] += len(crew)
                for guest in guests:
                    missing["guest total"] += 1
                    missing["guest blank name"] += not (guest.get("name") or "").strip()
                    missing["guest blank character"] += not (guest.get("character") or "").strip()
                    missing["guest missing order"] += "order" not in guest
                    missing["guest missing credit_id"] += not guest.get("credit_id")
                for member in crew:
                    missing["crew total"] += 1
                    missing["crew blank name"] += not (member.get("name") or "").strip()
                    missing["crew blank department"] += not (member.get("department") or "").strip()
                    missing["crew blank job"] += not (member.get("job") or "").strip()
                    missing["crew missing credit_id"] += not member.get("credit_id")

            standalone_by_id = {}
            for episode in standalone_episodes:
                guests = episode.get("guest_stars") or []
                crew = episode.get("crew") or []
                standalone_guest_keys |= _keys(guests)
                standalone_crew_keys |= _keys(crew)
                totals["standalone_guests"] += len(guests)
                totals["standalone_crew"] += len(crew)
                standalone_by_id[episode.get("id")] = (len(guests), len(crew))

            if len(appended_episodes) != len(standalone_episodes):
                episode_count_mismatch.append(
                    f"{label}: appended {len(appended_episodes)} episodes, "
                    f"standalone {len(standalone_episodes)}"
                )
            for episode in appended_episodes:
                counts = (
                    len(episode.get("guest_stars") or []),
                    len(episode.get("crew") or []),
                )
                expected = standalone_by_id.get(episode.get("id"))
                if expected is not None and expected != counts:
                    per_episode_mismatch.append(
                        f"{label} ep {episode.get('id')}: appended {counts}, standalone {expected}"
                    )

            print(
                f"{label}: appended season/{PROBE_SEASON} -> "
                f"{len(appended_episodes)} episodes, "
                f"{sum(len(e.get('guest_stars') or []) for e in appended_episodes)} guest stars, "
                f"{sum(len(e.get('crew') or []) for e in appended_episodes)} crew"
            )

    print()
    print("=== Q1: does the appended season block carry episode credits? ===")
    print(f"appended  guest stars: {totals['appended_guests']}, crew: {totals['appended_crew']}")
    print(
        f"standalone guest stars: {totals['standalone_guests']}, "
        f"crew: {totals['standalone_crew']}"
    )
    free = totals["appended_guests"] > 0 or totals["appended_crew"] > 0
    print(f"VERDICT: episode credits ride the appended block: {free}")
    if free:
        parity = (
            totals["appended_guests"] == totals["standalone_guests"]
            and totals["appended_crew"] == totals["standalone_crew"]
        )
        print(f"         counts match the standalone season fetch: {parity}")

    print()
    print("=== Q2: is the appended episode list complete? ===")
    print(f"episode-count mismatches: {episode_count_mismatch or 'none'}")
    print(f"per-episode credit mismatches: {per_episode_mismatch or 'none'}")

    print()
    print("=== Q3: which credit fields are missing or blank? ===")
    for label in sorted(missing):
        print(f"{label}: {missing[label]}")

    print()
    print("=== Q4: measured key sets ===")
    print(f"appended   guest_stars[]: {sorted(appended_guest_keys)}")
    print(f"standalone guest_stars[]: {sorted(standalone_guest_keys)}")
    print(f"appended   crew[]:        {sorted(appended_crew_keys)}")
    print(f"standalone crew[]:        {sorted(standalone_crew_keys)}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
