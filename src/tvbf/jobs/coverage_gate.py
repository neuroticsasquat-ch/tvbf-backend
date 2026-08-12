"""The pre-cutover go/no-go, as a CLI (NEU-1048).

    python -m tvbf.jobs.coverage_gate

A CLI for the same reason `show_prune`, `season_dedupe`, `episode_map` and
`human_queue` are: a migration-window operation run by hand a handful of times,
with no cursor to advance and nothing to poll. The process *is* the run, so the
**exit code is the verdict**. `tvbf.tmdb.coverage_gate` holds the criteria and
the coverage comparison; this is argparse over them. It needs no TMDB credential
— every question here is answered in Postgres.

**It writes nothing.** Reading is the whole job. Run it as often as it takes to
get to a go.

**JSON to stdout and nothing else**, like `reconcile capture`, `human_queue list`,
`episode_map report`, `season_dedupe report` and `show_prune report`: `docs/` is
not in the production image and a Coolify container is replaced on every deploy,
so the artifact has to travel over `ssh 'docker exec ...'`. The verdict, the
failed criteria and the advisory buckets are logged to **stderr**, so a terminal
run reads as prose while a redirect captures a clean artifact.

**Exit codes: 0 go, 1 no-go, 2 the gate could not run.** The third is separate on
purpose — a crashed gate must never be filed as a considered verdict — and both
non-zero codes fail closed.

**Re-runnable, and the artifact is the regression check.** The JSON is
deterministic (sorted buckets, sorted keys), so two runs of an unchanged database
are byte-identical and `git diff` over a saved copy is what shows a language
bucket getting worse. That is the same property `reconciliation-baseline.json`
relies on, for the same reason.
"""

import argparse
import asyncio
import json
import logging
import sys

from tvbf.config import get_settings
from tvbf.db import SessionLocal
from tvbf.logging_config import configure_logging
from tvbf.tmdb.coverage_gate import (
    ADVISORY_ABSENT_PCT,
    ADVISORY_MIN_BUCKET,
    MIN_INGESTED_SHOWS,
    GateReport,
    build_gate_report,
)

log = logging.getLogger(__name__)

# Reserved for "the gate could not run", so automation can tell a considered
# no-go from an exception. See the module docstring.
EXIT_ERROR = 2


def _log_verdict(report: GateReport) -> None:
    for criterion in report.criteria:
        log.log(
            logging.INFO if criterion.passed else logging.ERROR,
            "%s %s — %s",
            "PASS" if criterion.passed else "FAIL",
            criterion.name,
            criterion.description,
        )

    totals = report.to_dict()["coverage"]["totals"]
    log.info(
        "coverage: %d TV Maze show(s) — %d carried (%d matched), %d dropped "
        "(%d duplicated by an ingested title, %d with no counterpart)",
        totals["tvmaze_shows"],
        totals["carried"],
        totals["carried_matched"],
        totals["dropped"],
        totals["dropped_with_title_twin"],
        totals["dropped_without_title_twin"],
    )

    for axis, buckets in (("language", report.advisory_languages), ("era", report.advisory_eras)):
        if not buckets:
            continue
        # Advisory, and said out loud anyway. NEU-1066 accepted this loss
        # deliberately, so it cannot fail the run — but a bucket that is thin for
        # a *new* reason looks identical here, and the only way to tell is to
        # diff this artifact against the previous run.
        log.warning(
            "advisory: %d %s bucket(s) over %d shows are more than %.0f%% absent — %s. "
            "Accepted by NEU-1066 unless this is worse than the last run; diff the artifact",
            len(buckets),
            axis,
            ADVISORY_MIN_BUCKET,
            ADVISORY_ABSENT_PCT,
            ", ".join(buckets),
        )

    if report.failed:
        log.error("NO-GO — %s", ", ".join(report.failed))
    else:
        log.info("GO — every criterion passed")


async def run(args: argparse.Namespace) -> int:
    """The whole job, minus argument parsing. Returns the process exit code.

    Split out from `main` so tests can await it on their own event loop —
    `main`'s `asyncio.run` would otherwise rebuild the loop under the shared
    engine's pooled connections.
    """
    async with SessionLocal() as session:
        report = await build_gate_report(session, min_ingested=args.min_ingested)

    # The artifact, and nothing else, on stdout.
    sys.stdout.write(json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n")
    _log_verdict(report)
    return 1 if report.failed else 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m tvbf.jobs.coverage_gate")
    parser.add_argument(
        "--min-ingested",
        type=int,
        default=MIN_INGESTED_SHOWS,
        # A test seam, and nothing production ever passes: seeding 150,000 synced
        # shows to exercise four criteria is not a trade worth making.
        help=argparse.SUPPRESS,
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level)
    try:
        return asyncio.run(run(args))
    except Exception:
        log.exception("the pre-cutover gate could not run")
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
