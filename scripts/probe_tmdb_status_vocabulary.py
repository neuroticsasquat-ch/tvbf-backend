"""Measure TMDB's real `status` vocabulary, and where our `To Be Determined` shows land.

NEU-1031's audit settled on storing TMDB's status string verbatim, but it did so
against a 7-show sample that contained only `Returning Series` and `Ended`.
`Planned`, `In Production` and `Canceled` were documented rather than observed,
and the 5,037 shows we currently call `To Be Determined` have no TMDB
counterpart at all — where they land is a property of TMDB's data, not of ours,
so the only way to know is to read it. NEU-1037 hard-codes the resulting filter
list in the SPA, so it gets measured first.

The sweep also answers two smaller questions off the same payloads, because they
ride the request for free:

  - whether TMDB reuses a `season_number` within one show, which is what would
    make `UNIQUE (show_id, season_number)` unsafe on `catalog.season` (TV Maze
    does this, and it is why `tvmaze.season` carries no such constraint);
  - whether `in_production` genuinely carries information `status` does not.

Run inside the container, with `TMDB_READ_ACCESS_TOKEN` set:

    docker compose exec tvbf-backend python scripts/probe_tmdb_status_vocabulary.py

Costs two requests per sampled show — a `/find` to resolve our external id to a
TMDB series, then the series itself. At the default 250 per status that is ~2,000
requests, about two minutes at 20 req/s. Sampling is deterministic (`md5(id)`
ordering), so a re-run reads the same shows.
"""

import asyncio
import sys
from collections import Counter, defaultdict

from sqlalchemy import text

from tvbf.config import get_settings
from tvbf.db import SessionLocal
from tvbf.logging_config import configure_logging
from tvbf.tmdb.client import TMDBClient

# Per TV Maze status. Big enough that a status appearing in none of them is
# genuinely rare rather than unlucky; small enough to stay a two-minute probe.
SAMPLE_PER_STATUS = 250

# Ours, in mirror-count order. `To Be Determined` is the one the audit flagged.
TVMAZE_STATUSES = ("Ended", "Running", "To Be Determined", "In Development")

_SAMPLE_SQL = text("""
    SELECT id, name, externals_tvdb, externals_imdb
    FROM tvmaze.show
    WHERE status = :status
      AND (externals_tvdb IS NOT NULL OR externals_imdb IS NOT NULL)
    ORDER BY md5(id::text)
    LIMIT :limit
""")


async def _sample(status: str, limit: int) -> list[dict]:
    async with SessionLocal() as session:
        result = await session.execute(_SAMPLE_SQL, {"status": status, "limit": limit})
        return [
            {"id": r.id, "name": r.name, "tvdb": r.externals_tvdb, "imdb": r.externals_imdb}
            for r in result
        ]


async def _resolve(client: TMDBClient, show: dict) -> int | None:
    """Our show -> a TMDB series id, by exact external id only.

    Tiers 1 and 2 of the migration's mapping (project spec) and nothing else: a
    title match could put a wrong show's status in the tally, which is the one
    thing this probe must not do.
    """
    for value, source in ((show["tvdb"], "tvdb_id"), (show["imdb"], "imdb_id")):
        if value is None:
            continue
        payload = await client.find_by_external_id(str(value), source)
        results = payload.get("tv_results") or []
        if len(results) == 1:
            return results[0]["id"]
    return None


def _duplicate_season_numbers(payload: dict) -> list[int]:
    counts = Counter(s.get("season_number") for s in payload.get("seasons") or [])
    return sorted(n for n, c in counts.items() if c > 1 and n is not None)


async def main() -> int:
    configure_logging("WARNING")
    settings = get_settings()

    # status -> Counter of TMDB statuses
    landed: dict[str, Counter] = defaultdict(Counter)
    # TMDB status -> Counter of in_production
    in_production: dict[str, Counter] = defaultdict(Counter)
    unresolved: Counter = Counter()
    dup_seasons: list[tuple[str, int, list[int]]] = []
    sampled = 0

    async with TMDBClient(
        base_url=settings.tmdb_base_url,
        read_access_token=settings.tmdb_read_access_token,
        rate_calls=settings.tmdb_rate_limit_requests,
        rate_window=settings.tmdb_rate_limit_window_seconds,
        retry_max_attempts=settings.tmdb_retry_max_attempts,
    ) as client:
        for ours in TVMAZE_STATUSES:
            shows = await _sample(ours, SAMPLE_PER_STATUS)
            print(f"{ours}: sampling {len(shows)} shows", file=sys.stderr, flush=True)
            for show in shows:
                sampled += 1
                series_id = await _resolve(client, show)
                if series_id is None:
                    unresolved[ours] += 1
                    continue
                payload = await client.get_tv_series(series_id, append=())
                theirs = payload.get("status") or "(null)"
                landed[ours][theirs] += 1
                in_production[theirs][bool(payload.get("in_production"))] += 1
                dups = _duplicate_season_numbers(payload)
                if dups:
                    dup_seasons.append((payload.get("name", "?"), series_id, dups))

    print(f"\nSampled {sampled} shows across {len(TVMAZE_STATUSES)} of our statuses.\n")

    print("Where our shows land in TMDB's vocabulary")
    print("-" * 72)
    for ours in TVMAZE_STATUSES:
        total = sum(landed[ours].values())
        print(f"\n  {ours}  (resolved {total}, unresolved {unresolved[ours]})")
        for theirs, count in landed[ours].most_common():
            share = 100 * count / total if total else 0
            print(f"    {theirs:<20} {count:>5}  {share:5.1f}%")

    print("\n\nTMDB status values observed, and `in_production` alongside")
    print("-" * 72)
    overall: Counter = Counter()
    for counter in landed.values():
        overall.update(counter)
    for theirs, count in overall.most_common():
        flags = in_production[theirs]
        print(f"  {theirs:<20} {count:>5}   in_production true={flags[True]} false={flags[False]}")

    # Counted over the *resolved* shows, not everything sampled: an unresolved
    # show was never fetched, so it can neither show the quirk nor vouch for its
    # absence. Reporting it against `sampled` would overstate the evidence by a
    # third.
    resolved = sum(sum(counter.values()) for counter in landed.values())
    print("\n\nDuplicate season_number within one show")
    print("-" * 72)
    if dup_seasons:
        for name, series_id, dups in dup_seasons:
            print(f"  {name} (tmdb {series_id}): {dups}")
        print(f"\n  {len(dup_seasons)} of {resolved} resolved shows. UNIQUE would be unsafe.")
    else:
        print(f"  None in {resolved} resolved shows ({sampled} sampled).")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
