"""Episode-grain re-point and its report, as a CLI (NEU-1126).

    python -m tvbf.jobs.episode_repoint repoint [--limit N]
    python -m tvbf.jobs.episode_repoint report

A CLI for the same reason `season_dedupe` is: a migration-window operation run
by hand a handful of times, with no cursor to advance and nothing to poll. The
process *is* the run, so the exit code is the result.
`tvbf.tmdb.episode_repoint` holds the pass and the report; this is argparse over
them. It needs no TMDB credential — every question here is answered in Postgres.

**This is the one migration pass that writes to `app`.** `season_dedupe`
deliberately avoids it, as `show_prune` did before NEU-1051 deleted it; this one
moves 6,948 watch, rating and activity rows onto the ingested episode they should
have pointed at all along. Run
`report` first, and read `user_touched_repointable` and `user_touched_kept` before
spending it.

**Run it after NEU-1046 and before NEU-1047.** The foreign keys have to reference
`catalog.episode` first — every ingested episode id is far above TV Maze's range,
so the update would be rejected against the old constraint. And it has to land
before the read paths move, or the duplicated grain is visible to users on every
show and season page.

**This is no longer reversible.** The revert began with `task copy:catalog`,
which restored the deleted episode rows under their original ids but never
touched `app` — so it always needed a second statement per write site, recorded
in `tvbf.tmdb.episode_repoint`'s module docstring. NEU-1051 deleted that pass with
the `tvmaze` schema, leaving the pre-drop dump as the only source for the first
half.

**Exit codes: 0 = the pass completed, 1 = it aborted, raised, or refused to run
before the full TMDB ingest.** Episodes left
behind are not a failure — the ones with no TMDB counterpart are the
locally-authored residue ADR-0008 sanctions, and deleting one destroys history
nothing can restore. `repointable` reaching zero says the pass has nothing left to
do, not that the episode grain is clean; `still_doubled` is what scores the latter.

**`report` writes JSON to stdout and nothing else**, like `reconcile capture`,
`human_queue list`, `episode_map report` and `season_dedupe report`: `docs/` is not
in the production image and a Coolify container is replaced on every deploy, so the
artifact has to travel over `ssh 'docker exec ...'`. Logs go to stderr.

`--limit N` retires at most N episodes, which is how to try a hundred before
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
from tvbf.tmdb.episode_repoint import (
    MIN_INGESTED_SHOWS,
    IngestNotRun,
    build_report,
    ingested_show_count,
    repoint_episodes,
)

log = logging.getLogger(__name__)


async def _repoint(limit: int | None) -> int:
    async with SessionLocal() as session:
        try:
            result = await repoint_episodes(session, limit=limit)
        except IngestNotRun as exc:
            # A readable line rather than a stack trace, as `show_prune` did
            # before NEU-1051 deleted it: the operator's next move is to run the
            # ingest, not to read a traceback.
            log.error("%s", exc)
            return 1
        report = await build_report(session)

    log.info(
        "re-pointed %d watch(es), %d rating(s) and %d activity event(s), then deleted "
        "%d copied episode(s) over %d batch(es)",
        result.watches_repointed,
        result.ratings_repointed,
        result.activity_repointed,
        result.episodes_deleted,
        result.batches,
    )
    if result.blocked_by_collision:
        # The copy stays, and so does the user's row on it. Loud because it means
        # somebody holds records on both sides of a pair, which nothing else here
        # would tell them.
        log.warning(
            "%d copied episode(s) kept: a user already holds a row on the ingested twin, "
            "so moving theirs would have merged two records into one",
            result.blocked_by_collision,
        )
    if report.repointable:
        # Only reachable under `--limit`; a full pass runs the work list dry.
        log.info("%d episode(s) still to re-point — re-run to finish", report.repointable)
    log.info(
        "kept %d episode(s) with no TMDB counterpart, %d ambiguous, and %d under "
        "locally-authored shows",
        report.kept_no_counterpart,
        report.kept_ambiguous_copies + report.kept_ambiguous_twins,
        report.kept_under_unmatched_show,
    )
    return 0


async def _report() -> int:
    async with SessionLocal() as session:
        report = await build_report(session)
        ingested_count = await ingested_show_count(session)

    # The artifact, and nothing else, on stdout.
    sys.stdout.write(json.dumps(report.to_dict(), indent=2) + "\n")

    log.info(
        "%d copied episode(s) to re-point and delete, moving %d watch(es), %d rating(s) "
        "and %d activity event(s); %d user-touched episode(s) move and %d stay",
        report.repointable,
        report.watches_to_move,
        report.ratings_to_move,
        report.activity_to_move,
        report.user_touched_repointable,
        report.user_touched_kept,
    )
    log.info(
        "keeping %d with no TMDB counterpart, %d where two copies share one twin, "
        "%d where two ingested rows share one key, %d under locally-authored shows",
        report.kept_no_counterpart,
        report.kept_ambiguous_copies,
        report.kept_ambiguous_twins,
        report.kept_under_unmatched_show,
    )
    ingested = ingested_count
    if ingested < MIN_INGESTED_SHOWS:
        # `report` deliberately carries no floor — it is the half you run to
        # decide, including on a database where the pass would refuse. But a
        # pre-ingest reading looks like a nearly clean grain, which is the exact
        # misread `IngestNotRun` exists to prevent, so it says so.
        log.warning(
            "only %d show(s) carry a tmdb_synced_at, under the floor of %d — these counts "
            "read low because the ingest has not run, not because the grain is clean; "
            "`repoint` will refuse until it has",
            ingested,
            MIN_INGESTED_SHOWS,
        )
    if report.still_doubled:
        # The residue the pass cannot reach, said out loud so a zero in
        # `repointable` is never mistaken for a clean episode grain.
        carrying = sum(1 for row in report.still_doubled if row["carries_user_data"])
        log.warning(
            "%d (show, season, episode) key(s) stay doubled and this pass cannot fix them, "
            "%d carrying user data — read `still_doubled`: `ingested_rows` 0 is TV Maze's own "
            "duplicate numbering on a key TMDB has no episode for, 1 is that duplicate where "
            "TMDB does have it, above 1 is two rows the ingest wrote for one key",
            len(report.still_doubled),
            carrying,
        )
    return 0


async def run(args: argparse.Namespace) -> int:
    """The whole job, minus argument parsing. Returns the process exit code.

    Split out from `main` so tests can await it on their own event loop —
    `main`'s `asyncio.run` would otherwise rebuild the loop under the shared
    engine's pooled connections.
    """
    if args.mode == "repoint":
        return await _repoint(args.limit)
    return await _report()


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m tvbf.jobs.episode_repoint")
    modes = parser.add_subparsers(dest="mode", required=True)

    repoint = modes.add_parser(
        "repoint", help="move user history onto the ingested twin, then delete the copy"
    )
    repoint.add_argument(
        "--limit",
        type=int,
        help="retire at most this many copied episodes (for a smoke run)",
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
        log.exception("episode-grain re-point failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
