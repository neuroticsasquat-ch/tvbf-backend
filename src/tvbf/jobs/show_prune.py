"""Show-grain pruning and its report, as a CLI (NEU-1066).

    python -m tvbf.jobs.show_prune prune [--limit N]
    python -m tvbf.jobs.show_prune report

A CLI for the same reason `season_dedupe`, `episode_map` and `human_queue` are:
a migration-window operation run by hand a handful of times, with no cursor to
advance and nothing to poll. The process *is* the run, so the exit code is the
result. `tvbf.tmdb.show_prune` holds the pass and the report; this is argparse
over them. It needs no TMDB credential — every question here is answered in
Postgres.

**Run `report` first.** The pass deletes shows, and the report says exactly how
many, how much of the spine goes with them, and what it is leaving alone,
without writing anything.

**Run `prune` after the full TMDB ingest.** Before it, no copied row holds a
`tmdb_id` and the work list is the entire mirror; `prune_shows` refuses outright
rather than trusting the operator to know that, which is what `IngestNotRun`
reports. Re-running after a later ingest or delta is cheap and occasionally
useful: neither creates a copied row, but both can make one's ingested twin
appear.

**`task copy:catalog` puts the deleted rows back** — the copy is idempotent and
id-preserving, and `tvmaze` stands until NEU-1051 — so the work is reversible in
one command, seasons and episodes included. It does not restore a `tmdb_id`,
which is right: these rows never had one.

**Exit codes: 0 = the pass completed, 1 = it aborted or raised.** Shows left
behind are not a failure — the ones a user has touched are the data the migration
exists to protect. `deletable` reaching zero says the pass has nothing left to do;
`still_doubled` is what says whether the show grain is actually clean.

**`report` writes JSON to stdout and nothing else**, like `reconcile capture`,
`human_queue list`, `episode_map report` and `season_dedupe report`: `docs/` is
not in the production image and a Coolify container is replaced on every deploy,
so the artifact has to travel over `ssh 'docker exec ...'`. Logs go to stderr.

`--limit N` deletes at most N shows, which is how to try a hundred before
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
from tvbf.tmdb.show_prune import IngestNotRun, PruneReport, build_report, prune_shows

log = logging.getLogger(__name__)


async def _prune(limit: int | None) -> int:
    async with SessionLocal() as session:
        try:
            result = await prune_shows(session, limit=limit)
        except IngestNotRun as exc:
            log.error("%s", exc)
            return 1
        report = await build_report(session)

    log.info(
        "deleted %d unmatched copied show(s) over %d batch(es)",
        result.shows_deleted,
        result.batches,
    )
    if report.deletable:
        # Only reachable under `--limit`; a full pass runs the work list dry.
        log.info("%d show(s) still to go — re-run to finish", report.deletable)
    log.info(
        "kept %d show(s) a user has touched, %d ruled locally-authored by hand, "
        "%d not copied from TV Maze",
        report.kept_user_touched,
        report.kept_human_verdict,
        report.kept_not_copied,
    )
    _warn_if_doubled(report)
    return 0


async def _report() -> int:
    async with SessionLocal() as session:
        report = await build_report(session)

    # The artifact, and nothing else, on stdout.
    sys.stdout.write(json.dumps(report.to_dict(), indent=2) + "\n")

    log.info(
        "%d unmatched copied show(s) to delete, taking %d season(s) and %d episode(s); "
        "keeping %d a user has touched, %d ruled locally-authored by hand, %d not copied",
        report.deletable,
        report.seasons_to_delete,
        report.episodes_to_delete,
        report.kept_user_touched,
        report.kept_human_verdict,
        report.kept_not_copied,
    )
    log.info(
        "of those, %d duplicate an ingested row by title and %d have no counterpart there — "
        "the split the first acceptance criterion asks for",
        report.deletable_with_title_twin,
        report.deletable_without_title_twin,
    )
    _warn_if_doubled(report)
    return 0


def _warn_if_doubled(report: PruneReport) -> None:
    """Say out loud that the pass left a show visible twice, if it did.

    An empty work list is not the acceptance criterion — *"no show appears twice
    in browse or search"* is, and a spared duplicate is precisely the case where
    the two diverge. Warned rather than reported quietly, because resolving one
    means moving user history by hand and nothing else in the migration will
    surface it.
    """
    if not report.still_doubled:
        return
    log.warning(
        "%d kept show(s) still share a title with an ingested row and will appear twice — "
        "read `still_doubled`: each needs its user history moved onto the ingested id by "
        "hand, which `queue:confirm` cannot do post-ingest (uq_show_tmdb_id refuses)",
        len(report.still_doubled),
    )


async def run(args: argparse.Namespace) -> int:
    """The whole job, minus argument parsing. Returns the process exit code.

    Split out from `main` so tests can await it on their own event loop —
    `main`'s `asyncio.run` would otherwise rebuild the loop under the shared
    engine's pooled connections.
    """
    if args.mode == "prune":
        return await _prune(args.limit)
    return await _report()


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m tvbf.jobs.show_prune")
    modes = parser.add_subparsers(dest="mode", required=True)

    prune = modes.add_parser("prune", help="delete the unmatched, untouched copied shows")
    prune.add_argument(
        "--limit",
        type=int,
        help="delete at most this many shows (for a smoke run)",
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
        log.exception("show-grain pruning failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
