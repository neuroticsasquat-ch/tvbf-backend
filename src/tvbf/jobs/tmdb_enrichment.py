"""Attach `tmdb_id` to the copied catalog rows, as a CLI (NEU-1043).

    python -m tvbf.jobs.tmdb_enrichment [--limit N]

A CLI rather than an admin endpoint for the same reason `catalog_copy` was one
before NEU-1051 deleted it: a migration-window operation run by hand a handful
of times, with no cursor to advance and nothing to poll. The process *is* the
run, so the exit code is the result.

**Exit codes: 0 = the pass completed, 1 = it raised.** Unmatched shows are not a
failure — they are the expected output, and NEU-1044's human queue and
NEU-1045's episode-grain report are what consume them. A run that matched
nothing at all and a run that matched everything both exit 0; the counts in the
log are the thing to read.

It ran **after** `task copy:catalog` (deleted in NEU-1051) and **before** the
full TMDB ingest. At prod
scale it considers ~89k shows, with up to three upstream calls for a show that
falls through all three tiers. **Measured 3h28m** on 2026-08-10, not the 90
minutes first estimated from the 20 req/s budget: the loop is sequential, so
throughput is set by round-trip latency (~7-8 shows/sec) and the budget is never
the binding constraint. It commits every
500 shows and skips anything already mapped, so interrupting it costs at most the
current batch and re-running picks up where it stopped.

`--limit N` considers only the first N unmapped shows, which is how to try a
hundred before spending the full pass.
"""

import argparse
import asyncio
import logging
import sys

from tvbf.config import get_settings
from tvbf.db import SessionLocal
from tvbf.logging_config import configure_logging
from tvbf.tmdb.client import TMDBClient
from tvbf.tmdb.enrichment import (
    MATCH_IMDB_ID,
    MATCH_TITLE_YEAR,
    MATCH_TVDB_ID,
    EnrichmentResult,
    enrich_show_ids,
)

log = logging.getLogger(__name__)


async def run_enrichment(limit: int | None) -> EnrichmentResult:
    settings = get_settings()
    async with (
        SessionLocal() as session,
        TMDBClient(
            base_url=settings.tmdb_base_url,
            read_access_token=settings.tmdb_read_access_token,
            rate_calls=settings.tmdb_rate_limit_requests,
            rate_window=settings.tmdb_rate_limit_window_seconds,
            retry_max_attempts=settings.tmdb_retry_max_attempts,
        ) as client,
    ):
        return await enrich_show_ids(session, client, limit=limit)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m tvbf.jobs.tmdb_enrichment")
    parser.add_argument(
        "--limit",
        type=int,
        help="consider at most this many unmapped shows (for a smoke run)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level)
    try:
        result = asyncio.run(run_enrichment(args.limit))
    except Exception:
        log.exception("TMDB id enrichment failed")
        return 1

    log.info(
        "considered %d shows: %d matched (%d by tvdb_id, %d by imdb_id, %d by title+year), "
        "%d unmatched, %d collisions",
        result.considered,
        result.matched,
        result.by_method[MATCH_TVDB_ID],
        result.by_method[MATCH_IMDB_ID],
        result.by_method[MATCH_TITLE_YEAR],
        result.unmatched,
        result.collisions,
    )
    if result.unmatched or result.collisions:
        log.info(
            "%d shows still have no tmdb_id — that is the residue NEU-1044 and NEU-1045 work",
            result.unmatched + result.collisions,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
