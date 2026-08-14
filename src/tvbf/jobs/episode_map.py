"""Episode-grain mapping and its unmatched report, as a CLI (NEU-1045).

    python -m tvbf.jobs.episode_map map [--limit N]
    python -m tvbf.jobs.episode_map report

A CLI for the same reason `tmdb_enrichment` and `human_queue` are: a
migration-window operation run by hand a handful of times, with no cursor to
advance and nothing to poll. The process *is* the run, so the exit code is the
result. `tvbf.tmdb.episode_map` holds the pass and the report; this is argparse
over them.

**Run `map` after `task enrich:tmdb-ids` and before the full TMDB ingest.** Past
the ingest every upstream episode id is already held by an ingested row, so the
write is refused and the pass reports collisions instead of matches — see the
module docstring for why the ordering is what makes the ingest land on the rows
users are already attached to.

**Exit codes: 0 = the pass completed, 1 = it aborted or raised.** Unmatched
episodes are not a failure — they are the expected output, and the row that
`report` exists to surface. A run that mapped nothing and a run that mapped
everything both exit 0; the counts are the thing to read.

**`report` writes JSON to stdout and nothing else**, like `reconcile capture` and
`human_queue list`: `docs/` is not in the production image and a Coolify
container is replaced on every deploy, so the artifact has to travel over
`ssh 'docker exec ...'`. Logs go to stderr. It needs no TMDB credential, and it
exits 0 whether or not anything is unmatched — *empty* is what the cutover gate
reads out of it, not a failure.

`--limit N` considers only the first N shows, which is how to try a hundred
before spending the full pass.
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
from tvbf.tmdb.episode_map import build_report, map_episode_ids

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


async def _map(limit: int | None) -> int:
    async with SessionLocal() as session, _tmdb_client() as client:
        result = await map_episode_ids(session, client, limit=limit)

    episodes = result.episodes
    log.info(
        "considered %d show(s), %d failed (%d gone upstream): %d episodes mapped, "
        "%d unmatched, %d ambiguous, %d collisions, %d synthetic specials",
        result.shows_considered,
        result.shows_failed,
        result.shows_gone,
        episodes.matched,
        episodes.unmatched,
        episodes.ambiguous,
        episodes.collisions,
        episodes.synthetic,
    )
    if episodes.unmatched or episodes.ambiguous or episodes.collisions:
        log.info("run `report` for the unmatched episodes users have actually watched")
    return 0


async def _report() -> int:
    async with SessionLocal() as session:
        report = await build_report(session)

    # The artifact, and nothing else, on stdout.
    sys.stdout.write(json.dumps(report.to_dict(), indent=2) + "\n")

    totals = report.totals
    # Before the counts, because it is the one way this report can be wrong
    # rather than merely long: these watches have no catalog row at all, so every
    # other number below is silently computed without them.
    if report.unmirrored_watches:
        log.error(
            "%d watched episode(s) have no catalog.episode row and are NOT counted below; "
            "the copy that would have created them went with the tvmaze schema in "
            "NEU-1051, so these need authoring by hand: %s",
            len(report.unmirrored_watches),
            ", ".join(str(row["episode_id"]) for row in report.unmirrored_watches),
        )

    log.info(
        "%d of %d watched episode(s) are unmapped; %d unmapped row(s) carry watch or rating "
        "history, across %d show(s) where nothing mapped at all",
        totals["watched_episodes_unmapped"],
        totals["watched_episodes"],
        totals["unmatched_carrying_user_data"],
        totals["systematic_shows"],
    )
    if totals["systematic_shows"]:
        log.warning(
            "a show with no mapped episodes at all usually means the *show* matched the wrong "
            "TMDB series — check those before the individual misses"
        )
    return 0


async def run(args: argparse.Namespace) -> int:
    """The whole job, minus argument parsing. Returns the process exit code.

    Split out from `main` so tests can await it on their own event loop —
    `main`'s `asyncio.run` would otherwise rebuild the loop under the shared
    engine's pooled connections.
    """
    if args.mode == "map":
        return await _map(args.limit)
    return await _report()


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m tvbf.jobs.episode_map")
    modes = parser.add_subparsers(dest="mode", required=True)

    mapping = modes.add_parser("map", help="attach tmdb_id to the copied episode rows")
    mapping.add_argument(
        "--limit",
        type=int,
        help="consider at most this many shows (for a smoke run)",
    )

    modes.add_parser("report", help="report every unmatched episode carrying user data (JSON)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level)
    try:
        return asyncio.run(run(args))
    except Exception:
        log.exception("episode-grain mapping failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
