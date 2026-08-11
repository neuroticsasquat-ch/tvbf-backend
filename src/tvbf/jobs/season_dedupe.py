"""Season-grain deduplication and its report, as a CLI (NEU-1119).

    python -m tvbf.jobs.season_dedupe dedupe [--limit N]
    python -m tvbf.jobs.season_dedupe report

A CLI for the same reason `episode_map` and `human_queue` are: a migration-window
operation run by hand a handful of times, with no cursor to advance and nothing
to poll. The process *is* the run, so the exit code is the result.
`tvbf.tmdb.season_dedupe` holds the pass and the report; this is argparse over
them. It needs no TMDB credential — every question here is answered in Postgres.

**Run `report` first.** The pass deletes rows, and the report says exactly how
many and what it is leaving alone, without writing anything.

**Run `dedupe` after the full TMDB ingest, and re-run it after any later
ingest or delta.** A delta that adds a season to a matched show on a number a
copied row still holds is a fresh duplicate; there is no watermark, so a re-run
costs only what is genuinely still there.

**`task copy:catalog` puts the deleted rows back** — its anti-join verification
demands a catalog row per `tvmaze.season`, so it re-inserts each one under its
original id. It does not restore the episodes' parentage; see the two-statement
revert in `tvbf.tmdb.season_dedupe`'s module docstring, which is what keeps the
work reversible while `tvmaze` stands (NEU-1051 has not run).

**Exit codes: 0 = the pass completed, 1 = it aborted or raised.** Seasons left
behind are not a failure — the ones under an unmatched show are data the
migration is protecting, not residue. `deletable_duplicates` reaching zero says
the pass has nothing left to do, not that the season grain is clean; `still_doubled`
is what scores the latter.

**`report` writes JSON to stdout and nothing else**, like `reconcile capture`,
`human_queue list` and `episode_map report`: `docs/` is not in the production
image and a Coolify container is replaced on every deploy, so the artifact has to
travel over `ssh 'docker exec ...'`. Logs go to stderr.

`--limit N` deletes at most N seasons, which is how to try a hundred before
spending the full pass.
"""

import argparse
import asyncio
import json
import logging
import sys

from tvbf.config import get_settings
from tvbf.db import SessionLocal
from tvbf.logging_config import configure_logging
from tvbf.tmdb.season_dedupe import build_report, dedupe_seasons

log = logging.getLogger(__name__)


async def _dedupe(limit: int | None) -> int:
    async with SessionLocal() as session:
        result = await dedupe_seasons(session, limit=limit)
        report = await build_report(session)

    log.info(
        "deleted %d superseded season(s) over %d batch(es), re-pointing %d episode(s)",
        result.seasons_deleted,
        result.batches,
        result.episodes_repointed,
    )
    if report.deletable_duplicates:
        # Only reachable under `--limit`; a full pass runs the work list dry.
        log.info(
            "%d duplicate season(s) still to go — re-run to finish",
            report.deletable_duplicates,
        )
    if report.ambiguous:
        log.warning(
            "%d copied season(s) left in place: their show carries two ingested rows for that "
            "season number, so which one an episode belongs to has no answer",
            report.ambiguous,
        )
    log.info(
        "kept %d season(s) under locally-authored shows and %d with no TMDB counterpart",
        report.kept_under_unmatched_show,
        report.kept_no_counterpart,
    )
    return 0


async def _report() -> int:
    async with SessionLocal() as session:
        report = await build_report(session)

    # The artifact, and nothing else, on stdout.
    sys.stdout.write(json.dumps(report.to_dict(), indent=2) + "\n")

    log.info(
        "%d duplicate season(s) to delete, re-pointing %d episode(s) (%d carrying watch or "
        "rating history); keeping %d under locally-authored shows, %d with no TMDB counterpart, "
        "%d ambiguous",
        report.deletable_duplicates,
        report.episodes_to_repoint,
        report.episodes_carrying_user_data,
        report.kept_under_unmatched_show,
        report.kept_no_counterpart,
        report.ambiguous,
    )
    if report.still_doubled:
        # The residue the pass cannot reach, said out loud so a zero in
        # `deletable_duplicates` is never mistaken for a clean season grain.
        log.warning(
            "%d (show, season number) pair(s) stay doubled and this pass cannot fix them — "
            "read `still_doubled`: `show_matched` false is TV Maze's own duplicate numbering "
            "under a locally-authored show, `ingested_rows` above 1 is two rows the ingest "
            "wrote for one number",
            len(report.still_doubled),
        )
    return 0


async def run(args: argparse.Namespace) -> int:
    """The whole job, minus argument parsing. Returns the process exit code.

    Split out from `main` so tests can await it on their own event loop —
    `main`'s `asyncio.run` would otherwise rebuild the loop under the shared
    engine's pooled connections.
    """
    if args.mode == "dedupe":
        return await _dedupe(args.limit)
    return await _report()


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m tvbf.jobs.season_dedupe")
    modes = parser.add_subparsers(dest="mode", required=True)

    dedupe = modes.add_parser("dedupe", help="delete the copied seasons the ingest superseded")
    dedupe.add_argument(
        "--limit",
        type=int,
        help="delete at most this many seasons (for a smoke run)",
    )

    modes.add_parser("report", help="count what the pass would do and what it keeps (JSON)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level)
    try:
        return asyncio.run(run(args))
    except Exception:
        log.exception("season-grain deduplication failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
