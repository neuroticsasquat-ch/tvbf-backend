"""The recommendations backfill and its report, as a CLI (NEU-1052).

    python -m tvbf.jobs.recommendations_backfill backfill [--limit N]
    python -m tvbf.jobs.recommendations_backfill report

A CLI rather than an admin route, for the reason every other one-time pass here
is one: it is run by hand a handful of times, there is no cursor to advance and
nothing to poll. The process *is* the run, so the exit code is the result.
`tvbf.tmdb.recommendations_backfill` holds the pass and the report; this is
argparse over them.

**Exit codes: 0 = the pass completed, 1 = it aborted or raised.** A show TMDB
recommends nothing for is not a failure — it is stamped like any other and
counted apart, which is what stops the next run fetching it again.

**Stopping early is a supported outcome, not a failed run.** The work list is
ordered `popularity DESC`, so the top 20,000 shows — about 45 minutes — cover
essentially every page a user loads, and `--limit N` is how that is asked for
deliberately rather than by killing the process. Either way the watermark makes
a partial pass indistinguishable from a paused one: run it again and it picks up
where it stopped.

**`report` writes JSON to stdout and nothing else**, like `reconcile capture`
and `credits_backfill report`: `docs/` is not in the production image and a
Coolify container is replaced on every deploy, so the artifact has to travel over
`ssh 'docker exec ...'`. Logs go to stderr. It needs no TMDB credential and
writes nothing.
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
from tvbf.tmdb.recommendations_backfill import backfill_recommendations, build_report

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
        result = await backfill_recommendations(session, client, limit=limit)

    log.info(
        "considered %d show(s): %d stamped (%d row(s) written, %d target(s) dropped as "
        "unmirrored, %d had none upstream), %d failed (%d gone upstream)",
        result.shows_considered,
        result.shows_stamped,
        result.rows_written,
        result.targets_dropped,
        result.shows_without_recommendations,
        result.shows_failed,
        result.shows_gone,
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

    totals = report.totals
    log.info(
        "%d of %d mirrored show(s) have had recommendations written; %d left to fetch",
        totals["shows_stamped"],
        totals["shows_mirrored"],
        totals["shows_remaining"],
    )
    if report.user_touched_without_recommendations_total:
        log.warning(
            "%d show(s) a user tracks would still render no similar-shows section — the first "
            "%d are in the artifact, worst first",
            report.user_touched_without_recommendations_total,
            len(report.user_touched_without_recommendations),
        )
    return 0


async def run(args: argparse.Namespace) -> int:
    """The whole job, minus argument parsing. Returns the process exit code.

    Split out from `main` so tests can await it on their own event loop —
    `main`'s `asyncio.run` would otherwise rebuild the loop under the shared
    engine's pooled connections.
    """
    if args.mode == "backfill":
        return await _backfill(args.limit)
    return await _report()


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m tvbf.jobs.recommendations_backfill")
    modes = parser.add_subparsers(dest="mode", required=True)

    backfill = modes.add_parser(
        "backfill", help="write recommendations for every mirrored show with none, popular first"
    )
    backfill.add_argument(
        "--limit",
        type=int,
        help="consider at most this many shows, most popular first (a smoke run, or a "
        "deliberately partial pass)",
    )

    modes.add_parser("report", help="what the table holds and what is left (JSON)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level)
    try:
        return asyncio.run(run(args))
    except Exception:
        log.exception("recommendations backfill failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
