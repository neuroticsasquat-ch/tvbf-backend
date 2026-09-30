"""The season credits backfill and its report, as a CLI (NEU-1512).

    python -m tvbf.jobs.season_credits_backfill backfill [--limit N]
    python -m tvbf.jobs.season_credits_backfill report

`tvbf.jobs.credits_backfill` with the nouns changed, for the reasons that
module gives: run by hand, no cursor, nothing to poll, so the process is the
run and the exit code is the result. `tvbf.tmdb.season_credits_backfill` holds
the pass and the report; this is argparse over them.

**Exit codes: 0 = the pass completed, 1 = it aborted or raised.** A show whose
seasons list no regulars is stamped like any other.

**`report` writes JSON to stdout and nothing else**, so it can travel over
`ssh 'docker exec ...'` from production; logs go to stderr. Its
`shows_remaining` reaching zero is what NEU-1512's route change waits for.
"""

import argparse
import asyncio
import json
import logging
import sys

from tvbf.config import get_settings
from tvbf.db import SessionLocal
from tvbf.logging_config import configure_logging
from tvbf.tmdb.client import TMDBClient
from tvbf.tmdb.season_credits_backfill import backfill_season_credits, build_report

log = logging.getLogger(__name__)


def _tmdb_client() -> TMDBClient:
    settings = get_settings()
    return TMDBClient(
        base_url=settings.tmdb_base_url,
        read_access_token=settings.tmdb_read_access_token,
        rate_calls=settings.tmdb_rate_limit_requests,
        rate_window=settings.tmdb_rate_limit_window_seconds,
        retry_max_attempts=settings.tmdb_retry_max_attempts,
    )


async def _backfill(limit: int | None) -> int:
    async with SessionLocal() as session, _tmdb_client() as client:
        result = await backfill_season_credits(session, client, limit=limit)

    log.info(
        "considered %d show(s): %d written (%d seasons), %d failed (%d gone upstream, "
        "%d seasons without credits)",
        result.shows_considered,
        result.shows_stamped,
        result.seasons_written,
        result.shows_failed,
        result.shows_gone,
        result.seasons_without_credits,
    )
    if result.shows_failed > result.shows_gone:
        log.warning(
            "%d show(s) failed for reasons other than being gone upstream and were left "
            "unstamped — re-run to pick them up",
            result.shows_failed - result.shows_gone,
        )
    return 0


async def _report() -> int:
    async with SessionLocal() as session:
        report = await build_report(session)

    # The artifact, and nothing else, on stdout.
    sys.stdout.write(json.dumps(report.to_dict(), indent=2) + "\n")
    log.info(
        "%d of %d mirrored show(s) have had season regulars written; %d left to fetch",
        report.shows_stamped,
        report.shows_mirrored,
        report.shows_remaining,
    )
    return 0


async def run(args: argparse.Namespace) -> int:
    """The whole job, minus argument parsing. Returns the process exit code.

    Split out from `main` so tests can await it on their own event loop.
    """
    if args.mode == "backfill":
        return await _backfill(args.limit)
    return await _report()


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m tvbf.jobs.season_credits_backfill")
    modes = parser.add_subparsers(dest="mode", required=True)

    backfill = modes.add_parser(
        "backfill", help="write season regulars for every mirrored show without them"
    )
    backfill.add_argument(
        "--limit",
        type=int,
        help="consider at most this many shows (for a smoke run)",
    )

    modes.add_parser("report", help="what season_cast holds and what is left (JSON)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level)
    try:
        return asyncio.run(run(args))
    except Exception:
        log.exception("season credits backfill failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
