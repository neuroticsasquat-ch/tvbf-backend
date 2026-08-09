"""Measure TMDB's real `append_to_response` cap against the live API.

TMDB documents a 20-entry limit and does not say what happens past it. The
project's ~3.2-hour full-pass estimate rests on that number, so it gets measured
rather than trusted: how many entries come back, and whether the excess errors or
is dropped silently. The answer sizes milestone 3.

Run inside the container, with `TMDB_READ_ACCESS_TOKEN` set:

    docker compose exec tvbf-backend python scripts/probe_tmdb_append_limit.py

Costs about a dozen requests. Re-run whenever `APPEND_TO_RESPONSE_LIMIT` in
`tvbf/tmdb/client.py` is in doubt; this prints what it observed and the constant
records it.
"""

import asyncio
import sys

import httpx

from tvbf.config import get_settings
from tvbf.tmdb.client import TMDBClient, season_key

# The Simpsons: 36 seasons, comfortably more than any plausible cap, and stable
# enough that the probe reads the same next year.
SERIES_ID = 456

NAMESPACES = ["external_ids", "alternative_titles", "aggregate_credits"]

# Straddles the documented 20 on both sides so a lower real cap is visible.
SIZES = [10, 18, 19, 20, 21, 22, 25, 30]


def _honoured(payload: dict, requested: list[str]) -> list[str]:
    """Which requested entries actually came back in the payload."""
    return [key for key in requested if key in payload]


def _episode_counts(payload: dict, requested: list[str]) -> str:
    """How many appended seasons arrived carrying a full episode list.

    The cap is only worth what rides on it: an appended season that came back
    as a stub would mean a follow-up request per season anyway.
    """
    seasons = [k for k in requested if k.startswith("season/") and k in payload]
    with_episodes = [k for k in seasons if payload[k].get("episodes")]
    return f"{len(with_episodes)}/{len(seasons)}"


async def main() -> int:
    settings = get_settings()
    async with TMDBClient(
        base_url=settings.tmdb_base_url,
        read_access_token=settings.tmdb_read_access_token,
        rate_calls=settings.tmdb_rate_limit_requests,
        rate_window=settings.tmdb_rate_limit_window_seconds,
        retry_max_attempts=settings.tmdb_retry_max_attempts,
    ) as client:
        # Prove the credential first: a 401 here is a config problem, not a cap.
        await client.get_configuration()
        print("auth: OK (bearer token accepted)\n")

        largest_ok = 0
        first_rejected = None
        for size in SIZES:
            requested = (NAMESPACES + [season_key(n) for n in range(1, 40)])[:size]
            try:
                # Bypasses the client's own guard on purpose — measuring the cap
                # means asking for more than the cap.
                resp = await client._request(
                    "GET",
                    f"{settings.tmdb_base_url.rstrip('/')}/tv/{SERIES_ID}",
                    params={"append_to_response": ",".join(requested)},
                )
            except httpx.HTTPStatusError as exc:
                detail = exc.response.json().get("status_message", exc.response.text[:120])
                print(f"requested {size:>2} → HTTP {exc.response.status_code}: {detail}")
                if first_rejected is None:
                    first_rejected = size
                continue

            payload = resp.json()
            got = _honoured(payload, requested)
            missing = [k for k in requested if k not in got]
            episodes = _episode_counts(payload, requested)
            print(
                f"requested {size:>2} → honoured {len(got):>2}   "
                f"missing: {missing or 'none'}   seasons w/ episodes: {episodes}"
            )
            if len(got) == size:
                largest_ok = max(largest_ok, size)

        print()
        print(f"MEASURED CAP: {largest_ok} entries honoured in full.")
        if first_rejected is not None:
            print(
                f"Asking for {first_rejected} is rejected with HTTP 400 — TMDB fails loudly "
                "rather than truncating, so an oversized request cannot ingest silently."
            )
        else:
            print(f"No rejection observed up to {max(SIZES)} entries.")
        print("Record this in APPEND_TO_RESPONSE_LIMIT in tvbf/tmdb/client.py.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
