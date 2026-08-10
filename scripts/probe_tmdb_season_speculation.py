"""Measure whether a *speculative* `season/N` append is safe, and which window to guess.

The full-catalog ingest cannot know a show's season numbers before it has fetched
the show, but `append_to_response` has to be built before the request goes out.
So the first request guesses a window of season numbers and reconciles whatever
it got against `seasons[]` afterwards. Two things about that guess are worth
measuring rather than assuming, because the ~3.2-hour estimate rests on most
shows completing in one request:

1. **What does TMDB do with an appended season a show does not have?** If it
   drops the key silently, speculation is free. If it 400s the whole request,
   speculation would break the ingest for the majority of shows — every
   single-season series in the catalog.
2. **Which window pays best?** Guessing `0..8` wins on shows that carry specials;
   guessing `1..9` wins on shows with exactly nine numbered seasons. The
   distribution decides, and only one of the two is common.

Run inside the container, with `TMDB_READ_ACCESS_TOKEN` set:

    docker compose exec tvbf-backend python scripts/probe_tmdb_season_speculation.py

Costs one export download plus ~200 requests. Record what it prints in
`tvbf/tmdb/ingest.py`'s `SPECULATIVE_SEASONS`.
"""

import asyncio
import sys
from collections import Counter

import httpx

from tvbf.config import get_settings
from tvbf.tmdb.client import DEFAULT_APPEND, TMDBClient, season_key
from tvbf.tmdb.export import fetch_series_ids

# Deliberately spread: a one-season show is where an over-wide guess would break
# things, and a 36-season show is where the overflow path has to engage.
PROBE_SERIES = {
    456: "The Simpsons (36 seasons)",
    1396: "Breaking Bad (5 seasons + specials)",
    93405: "Squid Game (few seasons)",
}

# How many series to sample from the export for the distribution.
SAMPLE = 200


async def _probe_speculation(client: TMDBClient, base_url: str) -> None:
    print("=== 1. is a speculative season/N append safe? ===\n")
    window = list(range(0, 9))
    for series_id, label in PROBE_SERIES.items():
        requested = list(DEFAULT_APPEND) + [season_key(n) for n in window]
        try:
            payload = await client.get_tv_series(series_id, append=requested)
        except httpx.HTTPStatusError as exc:
            detail = exc.response.json().get("status_message", exc.response.text[:120])
            print(f"{label:<38} HTTP {exc.response.status_code}: {detail}")
            continue
        real = {s["season_number"] for s in payload.get("seasons", [])}
        asked_for_absent = [n for n in window if n not in real]
        came_back = [n for n in window if f"season/{n}" in payload]
        bogus_returned = [n for n in came_back if n not in real]
        print(
            f"{label:<38} 200 OK   real seasons: {sorted(real)[:12]}"
            f"{'…' if len(real) > 12 else ''}\n"
            f"{'':<38} asked for {len(asked_for_absent)} absent season(s) "
            f"{asked_for_absent} → {len(bogus_returned)} returned {bogus_returned}"
        )
    print(
        "\nSAFE if every row above is 200 OK and no absent season came back.\n"
        "UNSAFE if any row 4xx'd — speculation would then have to be abandoned.\n"
    )


async def _probe_distribution(client: TMDBClient, ids: list[int]) -> None:
    print(f"=== 2. season shape across {SAMPLE} sampled series ===\n")
    # Evenly spaced through the export rather than the first N: the file is
    # ordered by id, so the head is all long-established series.
    step = max(1, len(ids) // SAMPLE)
    sample = ids[::step][:SAMPLE]

    has_zero = 0
    counts: Counter[int] = Counter()
    fits_0_8 = 0
    fits_1_9 = 0
    ok = 0
    for series_id in sample:
        try:
            payload = await client.get_tv_series(series_id, append=[])
        except httpx.HTTPStatusError:
            continue
        numbers = {s["season_number"] for s in payload.get("seasons", [])}
        ok += 1
        if 0 in numbers:
            has_zero += 1
        counts[len(numbers)] += 1
        if numbers <= set(range(0, 9)):
            fits_0_8 += 1
        if numbers <= set(range(1, 10)):
            fits_1_9 += 1

    if not ok:
        print("no series fetched — nothing to report")
        return
    print(f"fetched:                  {ok}")
    print(f"carry a season 0:         {has_zero} ({has_zero / ok:.1%})")
    print(f"fully covered by 0..8:    {fits_0_8} ({fits_0_8 / ok:.1%})")
    print(f"fully covered by 1..9:    {fits_1_9} ({fits_1_9 / ok:.1%})")
    print("season-count histogram:   " + ", ".join(f"{k}:{v}" for k, v in sorted(counts.items())))
    print("\nThe higher coverage figure is the window to guess.")


async def main() -> int:
    settings = get_settings()
    ids = await fetch_series_ids()
    print(f"id export: {len(ids)} series\n")
    async with TMDBClient(
        base_url=settings.tmdb_base_url,
        read_access_token=settings.tmdb_read_access_token,
        rate_calls=settings.tmdb_rate_limit_requests,
        rate_window=settings.tmdb_rate_limit_window_seconds,
        retry_max_attempts=settings.tmdb_retry_max_attempts,
    ) as client:
        await client.get_configuration()
        await _probe_speculation(client, settings.tmdb_base_url)
        await _probe_distribution(client, ids)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
