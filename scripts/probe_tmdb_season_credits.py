"""Measure how a season's regular cast can be fetched, and what it looks like (NEU-1512 §3.1).

TMDB records regular cast **per season** (`GET /tv/{id}/season/{n}/credits`,
`cast[]`) and guest stars and crew per episode. The ingest appends `season/N`
to the series request, and that block's `episodes[]` carries `guest_stars` and
`crew` but no `cast` — so today no regular is tied to a season. NEU-1512 adds
the season list, and which of two fetch routes it takes turns on one fact
nobody has measured:

* **Route A** — `season/N/credits` rides the series `append_to_response` beside
  `season/N`. Season credits then cost no request of their own, and the append
  budget's season slots halve.
* **Route B** — it does not, and every season costs one standalone
  `/season/{n}/credits` request: ~397k on a full pass.

So the questions, in the spec's numbering:

1. Does `GET /tv/{id}?append_to_response=season/1,season/1/credits` return a
   `season/1/credits` key carrying `cast[]` — or an error, or nothing?
2. Does `GET /tv/{id}/season/1?append_to_response=credits` return `credits`?
   Route A's overflow seasons rely on it.
3. Key sets and gaps on `cast[]`: every key seen, entries missing `id` or
   `name`, blank `character`, `order` presence. Decides whether the payload
   class is strict or lenient on person identity (NEU-1128).
4. Parity: each season's regulars against that season's episode
   `guest_stars` (overlap) and against `aggregate_credits` (regulars with no
   aggregate row). Informational; sizes the edge cases in spec §4.

The standalone `/season/{n}/credits` response is the reference every other
answer is compared with, because it is the endpoint TMDB documents for this.

Run inside the container, with `TMDB_READ_ACCESS_TOKEN` set:

    docker compose exec api python scripts/probe_tmdb_season_credits.py

Costs 3 requests per probed season.
"""

import asyncio
import sys
from collections import Counter

import httpx

from tvbf.config import get_settings
from tvbf.tmdb.client import TMDBClient

# `probe_tmdb_episode_credits_append.py`'s five series, so the measurements are
# comparable, plus the largest cast in the catalog and a short show whose
# seasons can be compared with each other.
PROBE_SERIES: dict[int, tuple[str, tuple[int, ...]]] = {
    456: ("The Simpsons (animation, voice ensemble)", (1,)),
    1396: ("Breaking Bad (drama)", (1,)),
    1667: ("Saturday Night Live (sketch/variety)", (1,)),
    95479: ("Jujutsu Kaisen (anime)", (1,)),
    82856: ("The Mandalorian (genre drama)", (1,)),
    549: ("Law & Order (largest cast)", (1,)),
    67070: ("Fleabag (two seasons)", (1, 2)),
}


def _cast_ids(entries: list[dict]) -> list:
    return [entry.get("id") for entry in entries]


async def _get(client: TMDBClient, path: str, params: dict | None = None) -> dict | str:
    """A GET the client has no method for; an HTTP error comes back as text, not raised.

    Reaches `_request` on purpose: the probe exists to find out which of these
    requests should *become* a client method, so it cannot wait for one.
    """
    try:
        resp = await client._request("GET", f"{client._base_url}{path}", params=params)
    except httpx.HTTPStatusError as exc:
        return f"HTTP {exc.response.status_code}: {exc.response.text[:200]}"
    return resp.json()


async def main() -> int:
    q1: list[str] = []
    q2: list[str] = []
    cast_keys: set[str] = set()
    gaps: Counter = Counter()
    parity: list[str] = []

    settings = get_settings()
    async with TMDBClient(
        base_url=settings.tmdb_base_url,
        read_access_token=settings.tmdb_read_access_token,
        rate_calls=settings.tmdb_rate_limit_requests,
        rate_window=settings.tmdb_rate_limit_window_seconds,
        retry_max_attempts=settings.tmdb_retry_max_attempts,
    ) as client:
        for series_id, (label, seasons) in PROBE_SERIES.items():
            for n in seasons:
                reference = await _get(client, f"/tv/{series_id}/season/{n}/credits")
                if isinstance(reference, str):
                    print(f"{label} s{n}: standalone /season/{n}/credits failed: {reference}")
                    continue
                regulars = reference.get("cast") or []
                reference_ids = _cast_ids(regulars)

                # Q1 — the compound key on the series append. `aggregate_credits`
                # rides the same request for Q4's parity.
                appended = await _get(
                    client,
                    f"/tv/{series_id}",
                    {"append_to_response": f"aggregate_credits,season/{n},season/{n}/credits"},
                )
                if isinstance(appended, str):
                    q1.append(f"{label} s{n}: {appended}")
                    aggregate_ids: set = set()
                    guest_ids: set = set()
                else:
                    compound = appended.get(f"season/{n}/credits")
                    if compound is None:
                        q1.append(f"{label} s{n}: key absent")
                    else:
                        got = _cast_ids(compound.get("cast") or [])
                        q1.append(
                            f"{label} s{n}: present, {len(got)} cast "
                            f"(reference {len(reference_ids)}, identical={got == reference_ids})"
                        )
                    season = appended.get(f"season/{n}") or {}
                    q1_season_keys = sorted(season)
                    if "credits" in season:
                        q1.append(f"{label} s{n}: NOTE season/{n} itself carries `credits`")
                    q1.append(f"{label} s{n}: season/{n} keys {q1_season_keys}")
                    aggregate = appended.get("aggregate_credits") or {}
                    aggregate_ids = {entry.get("id") for entry in aggregate.get("cast") or []}
                    guest_ids = {
                        guest.get("id")
                        for episode in season.get("episodes") or []
                        for guest in episode.get("guest_stars") or []
                    }

                # Q2 — the standalone season with credits appended.
                standalone = await _get(
                    client,
                    f"/tv/{series_id}/season/{n}",
                    {"append_to_response": "credits"},
                )
                if isinstance(standalone, str):
                    q2.append(f"{label} s{n}: {standalone}")
                elif "credits" not in standalone:
                    q2.append(f"{label} s{n}: key absent")
                else:
                    got = _cast_ids(standalone["credits"].get("cast") or [])
                    q2.append(
                        f"{label} s{n}: present, {len(got)} cast "
                        f"(reference {len(reference_ids)}, identical={got == reference_ids})"
                    )

                # Q3 — shape of the reference list.
                for entry in regulars:
                    cast_keys |= set(entry)
                    gaps["total"] += 1
                    gaps["missing id"] += entry.get("id") is None
                    gaps["blank name"] += not (entry.get("name") or "").strip()
                    gaps["blank character"] += not (entry.get("character") or "").strip()
                    gaps["missing order"] += "order" not in entry
                    gaps["missing credit_id"] += not entry.get("credit_id")

                # Q4 — parity.
                ids = set(reference_ids)
                parity.append(
                    f"{label} s{n}: {len(ids)} regulars; "
                    f"{len(ids & guest_ids)} also guest-star in the season; "
                    f"{len(ids - aggregate_ids)} have no aggregate_credits row"
                )
                print(f"{label} s{n}: {len(regulars)} regulars")

    print()
    print("=== Q1: does season/N/credits ride the series append? ===")
    for line in q1:
        print(line)
    print()
    print("=== Q2: does the standalone season take append_to_response=credits? ===")
    for line in q2:
        print(line)
    print()
    print("=== Q3: cast[] key set and gaps ===")
    print(f"keys: {sorted(cast_keys)}")
    for key in sorted(gaps):
        print(f"{key}: {gaps[key]}")
    print()
    print("=== Q4: parity ===")
    for line in parity:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
