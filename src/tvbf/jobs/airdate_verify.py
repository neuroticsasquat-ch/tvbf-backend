"""Acceptance criteria 2 and 3 for the airdate correction, as a CLI (NEU-1145 §7).

    # Before the first reconciliation pass, against production. `VERIFY` is just
    # to keep these lines short; the real invocation is one `ssh ... docker exec`.
    VERIFY="docker exec -i <container> python -m tvbf.jobs.airdate_verify"
    ssh $PROD_SSH "$VERIFY capture" > docs/migration/neu-1145-pre-run-baseline.json

    # After it:
    ssh $PROD_SSH "$VERIFY verify --baseline -" < docs/migration/neu-1145-pre-run-baseline.json

    # AC 2's table, for the three shows the operator reported:
    python -m tvbf.jobs.airdate_verify shows

**Capture the baseline before the pass runs.** That ordering is the whole
mechanism, and it cannot be recovered afterwards: AC 3 asks whether the
already-correct rows were left alone, and after the fact a row that agrees looks
identical whether it always did or whether something broke it and something else
repaired it. `app.watch_archive` is append-only, so the baseline stays valid
however long the gap is — but it has to be taken first.

**Exit codes: 0 = nothing got worse, 1 = something did.** This deliberately
inverts `jobs/reconcile.py`, which fails on any difference in either direction
because during a cutover window nothing should move at all. Here movement is the
point — a row going from *a day early* to *agrees* is the ticket working — so
only a **regression** fails: a disagreement that grew, or a row that stopped
being comparable. `airdates/verify.py:_is_regression` states the rule once.

**Rows still reading a day early are not a failure**, and the count is printed
rather than scored. Some cannot be corrected and it is on purpose: a show TV
Maze has never heard of, and a season the trust rule refused — Shrinking S3 is
the worked example, and it is in the reconciliation's own refusal log with its
per-episode deltas.

**`shows` cannot assert.** AC 2 is *"hand-verified against Apple's published
schedule"*, and there is no machine-readable schedule to check against, so this
prints what a human compares: the date now served, the raw TMDB value it came
from, and the offset between them.

**The artifact travels on stdin and stdout**, never a path inside the container
— `docs/` is not in the production image and a Coolify container is replaced on
every deploy. Logs go to stderr so they cannot corrupt it.
"""

import argparse
import asyncio
import json
import logging
import sys
from typing import Any

from tvbf.airdates.verify import (
    AC2_SHOWS,
    build_snapshot,
    compare,
    count_archive_rows,
    load_archive_rows,
    show_report,
)
from tvbf.config import get_settings
from tvbf.db import SessionLocal
from tvbf.logging_config import configure_logging

log = logging.getLogger(__name__)


def dumps(payload: dict[str, Any]) -> str:
    """The one place the artifact's byte format is decided.

    `sort_keys` is what makes two runs of an unchanged database byte-identical,
    which is what "diffable" has to mean for a baseline that lives in git.
    """
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _load_baseline(path: str) -> dict[str, Any]:
    if path == "-":
        return json.load(sys.stdin)
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


async def _capture() -> int:
    async with SessionLocal() as session:
        rows = await load_archive_rows(session)
        total = await count_archive_rows(session)
    snapshot = build_snapshot(rows)

    sys.stdout.write(dumps(snapshot))

    totals = snapshot["totals"]
    log.info(
        "%d archived episode row(s) with a date: %d agree, %d one day early, "
        "%d one day late, %d other, %d undated, %d unresolved",
        total,
        totals["agrees"],
        totals["one_day_early"],
        totals["one_day_late"],
        totals["other"],
        totals["undated"],
        totals["unresolved"],
    )
    if totals["unresolved"]:
        # Before anything else: these rows are outside every count above, so a
        # percentage computed over the rest is quietly computed without them.
        log.warning(
            "%d archived row(s) no longer resolve to a catalog episode and are NOT scored — "
            "expected for history NEU-1146 retired, worth reading if it is more than a handful",
            totals["unresolved"],
        )
    return 0


async def _verify(baseline_path: str) -> int:
    baseline = _load_baseline(baseline_path)
    async with SessionLocal() as session:
        rows = await load_archive_rows(session)
    diff = compare(baseline, build_snapshot(rows))

    sys.stdout.write(dumps(diff))

    log.info(
        "%d row(s) corrected, %d still one day early, %d added since the baseline",
        len(diff["corrected"]),
        len(diff["still_early"]),
        len(diff["added"]),
    )
    for move in diff["other_movements"]:
        log.info(
            "archive row %d (%s S%sE%s) moved %s -> %s",
            move["archive_id"],
            move["show"],
            move["season"],
            move["episode"],
            move["from"],
            move["to"],
        )
    if not diff["regressed"]:
        log.info("no row got worse — AC 3's second claim holds")
        return 0

    for move in diff["regressed"]:
        log.error(
            "REGRESSION: archive row %d (%s S%sE%s) went %s -> %s (TV Maze %s, catalog now %s)",
            move["archive_id"],
            move["show"],
            move["season"],
            move["episode"],
            move["from"],
            move["to"],
            move["archived_airdate"],
            move["catalog_airdate"],
        )
    log.error(
        "%d row(s) got worse — this is the failure AC 3 exists to catch", len(diff["regressed"])
    )
    return 1


async def _shows(names: list[str]) -> int:
    async with SessionLocal() as session:
        report = await show_report(session, show_names=names or list(AC2_SHOWS))

    sys.stdout.write(dumps(report))

    for entry in report["shows"]:
        if not entry["matched"]:
            log.warning("no show named %r in the catalog", entry["requested"])
            continue
        for show in entry["matched"]:
            log.info(
                "%s (show %d): offsets %s",
                show["name"],
                show["show_id"],
                show["offsets"] or "none recorded",
            )
    log.info("hand-check these against the network's published schedule — AC 2 cannot be automated")
    return 0


async def run(args: argparse.Namespace) -> int:
    """The whole job, minus argument parsing. Returns the process exit code.

    Split out from `main` so tests can await it on their own event loop —
    `main`'s `asyncio.run` would otherwise rebuild the loop under the shared
    engine's pooled connections.
    """
    if args.mode == "capture":
        return await _capture()
    if args.mode == "verify":
        return await _verify(args.baseline)
    return await _shows(args.show)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m tvbf.jobs.airdate_verify")
    modes = parser.add_subparsers(dest="mode", required=True)

    modes.add_parser("capture", help="snapshot the watch_archive comparison as JSON (stdout)")

    verify = modes.add_parser("verify", help="diff the live database against a captured baseline")
    verify.add_argument(
        "--baseline",
        required=True,
        help="path to a captured snapshot, or - to read it from stdin",
    )

    shows = modes.add_parser("shows", help="AC 2's per-season table for named shows (JSON)")
    shows.add_argument(
        "--show",
        action="append",
        default=[],
        help=f"show name, repeatable (default: {', '.join(AC2_SHOWS)})",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level)
    try:
        return asyncio.run(run(args))
    except Exception:
        log.exception("airdate verification failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
