"""Measure the shape of TMDB's credit payloads before NEU-1038 models them.

Four questions the table definitions turn on, none of which can be answered from
documentation:

1. **Do show-crew and episode-crew share a role vocabulary?** `tvmaze` keeps two
   lookups because its two vocabularies are genuinely disjoint — 233
   production-function names on the show side against `Writer` / `Director` /
   `Story` / `Teleplay` on the episode side. NEU-1038's ticket says carry that
   forward. But TMDB emits `department` + `job` on *both* sides, so the premise
   may not survive the source change, and a second lookup holding the same pairs
   would be a duplicate rather than a distinction.
2. **Does either crew payload carry an `order`?** `tvmaze.show_crew` and
   `tvmaze.episode_crew` both have a NOT NULL `sort_order`. If TMDB gives no
   ordering for crew, that column has nothing to hold and `episode_count` is the
   sort key instead (NEU-1039).
3. **How often is `character` empty?** `tvmaze` made `character_id` NOT NULL
   because TV Maze always sent a character object. TMDB sends free text, and
   free text can be `""`.
4. **What are the exact key sets?** Same discipline as the other probes here —
   the inventory in the audit is a measurement, not a recollection.

Run inside the container, with `TMDB_READ_ACCESS_TOKEN` set:

    docker compose exec tvbf-backend python scripts/probe_tmdb_credit_shapes.py

Costs ~2 requests per probed series.
"""

import asyncio
import sys
from collections import Counter

from tvbf.config import get_settings
from tvbf.tmdb.client import TMDBClient

# Deliberately spread across the kinds of show whose credits differ most: a
# long-running animation with a voice ensemble, a prestige drama, a reality
# format with no characters at all, and an anime whose crew vocabulary is
# translated.
PROBE_SERIES = {
    456: "The Simpsons (animation, voice ensemble)",
    1396: "Breaking Bad (drama)",
    1667: "Saturday Night Live (sketch/variety)",
    95479: "Jujutsu Kaisen (anime)",
    82856: "The Mandalorian (genre drama)",
}

# The season fetched for episode-level credits on each series.
PROBE_SEASON = 1


def _keys(rows: list[dict]) -> set[str]:
    return {key for row in rows for key in row}


async def main() -> int:
    show_roles: set[tuple[str, str]] = set()
    episode_roles: set[tuple[str, str]] = set()
    show_crew_keys: set[str] = set()
    show_cast_keys: set[str] = set()
    roles_keys: set[str] = set()
    jobs_keys: set[str] = set()
    guest_keys: set[str] = set()
    episode_crew_keys: set[str] = set()
    crew_has_order: Counter = Counter()
    episode_crew_has_order: Counter = Counter()
    character_blank: Counter = Counter()
    guest_character_blank: Counter = Counter()

    settings = get_settings()
    async with TMDBClient(
        base_url=settings.tmdb_base_url,
        read_access_token=settings.tmdb_read_access_token,
        rate_calls=settings.tmdb_rate_limit_requests,
        rate_window=settings.tmdb_rate_limit_window_seconds,
        retry_max_attempts=settings.tmdb_retry_max_attempts,
    ) as client:
        for series_id, label in PROBE_SERIES.items():
            series = await client.get_tv_series(series_id, append=("aggregate_credits",))
            credits = series.get("aggregate_credits") or {}
            cast = credits.get("cast") or []
            crew = credits.get("crew") or []

            show_cast_keys |= _keys(cast)
            show_crew_keys |= _keys(crew)
            for member in crew:
                crew_has_order["order" in member] += 1
                jobs = member.get("jobs") or []
                jobs_keys |= _keys(jobs)
                for job in jobs:
                    show_roles.add((member.get("department") or "", job.get("job") or ""))
            for member in cast:
                roles = member.get("roles") or []
                roles_keys |= _keys(roles)
                for role in roles:
                    character_blank[not (role.get("character") or "").strip()] += 1

            season = await client.get_tv_season(series_id, PROBE_SEASON)
            for episode in season.get("episodes") or []:
                guests = episode.get("guest_stars") or []
                ecrew = episode.get("crew") or []
                guest_keys |= _keys(guests)
                episode_crew_keys |= _keys(ecrew)
                for guest in guests:
                    guest_character_blank[not (guest.get("character") or "").strip()] += 1
                for member in ecrew:
                    episode_crew_has_order["order" in member] += 1
                    episode_roles.add((member.get("department") or "", member.get("job") or ""))

            print(f"{label}: {len(cast)} cast, {len(crew)} crew entries")

    print()
    print("=== Q1: do show-crew and episode-crew share a role vocabulary? ===")
    overlap = show_roles & episode_roles
    print(f"show (department, job) pairs:    {len(show_roles)}")
    print(f"episode (department, job) pairs: {len(episode_roles)}")
    print(f"shared pairs:                    {len(overlap)}")
    if episode_roles:
        share = 100 * len(overlap) / len(episode_roles)
        print(f"share of episode pairs also seen at show level: {share:.1f}%")
    print(f"episode pairs: {sorted(episode_roles)}")
    print(f"shared: {sorted(overlap)}")

    print()
    print("=== Q2: does crew carry an `order`? ===")
    print(
        f"show crew entries with `order`:    {crew_has_order[True]} "
        f"of {sum(crew_has_order.values())}"
    )
    print(
        f"episode crew entries with `order`: {episode_crew_has_order[True]} "
        f"of {sum(episode_crew_has_order.values())}"
    )

    print()
    print("=== Q3: how often is `character` blank? ===")
    print(f"cast roles blank:  {character_blank[True]} of {sum(character_blank.values())}")
    print(
        f"guest stars blank: {guest_character_blank[True]} of {sum(guest_character_blank.values())}"
    )

    print()
    print("=== Q4: measured key sets ===")
    print(f"aggregate_credits.cast[]:  {sorted(show_cast_keys)}")
    print(f"  .roles[]:                {sorted(roles_keys)}")
    print(f"aggregate_credits.crew[]:  {sorted(show_crew_keys)}")
    print(f"  .jobs[]:                 {sorted(jobs_keys)}")
    print(f"episode.guest_stars[]:     {sorted(guest_keys)}")
    print(f"episode.crew[]:            {sorted(episode_crew_keys)}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
