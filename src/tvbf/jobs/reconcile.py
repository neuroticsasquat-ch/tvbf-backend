"""The reconciliation harness, as a CLI (NEU-1030).

    python -m tvbf.jobs.reconcile capture > docs/migration/reconciliation-baseline.json
    python -m tvbf.jobs.reconcile verify --baseline - < docs/migration/reconciliation-baseline.json

**The artifact travels on stdin and stdout, never as a path inside the
container.** `docs/` is neither mounted into the dev container nor copied into
the production image, and a Coolify container is replaced on every deploy, so a
file written *inside* one is unreachable and short-lived. `capture` therefore
writes JSON to stdout and nothing else (logs go to stderr), and `verify` accepts
`--baseline -` to read the baseline from stdin — which is what makes the whole
flow work over `ssh 'docker exec -i ...'` against prod.

`verify` diffs the live database against that baseline. **Exit codes are the
contract: 0 = nothing moved, 1 = something did.** Both directions fail; a gain
during a cutover window means something ran that should not have. Every
discrepancy is printed with the user and the show it belonged to.

`--spine catalog` points the episode→show joins at the new schema, which is how
the same baseline is re-checked after cutover.

**Merging this is not running it.** The post-repoint acceptance test (NEU-1125)
is a production run whose verdict gates NEU-1050 and NEU-1051, and a non-zero
exit adjudicated by hand is worth exactly as much as the argument written down
beside it — so every run is recorded in `docs/migration/README.md`, alongside the
query that tells a benign gain from a loss.
"""

import argparse
import asyncio
import json
import logging
import sys
from typing import Any

from tvbf.app.services.reconciliation_service import (
    DEFAULT_SPINE,
    SPINES,
    build_snapshot,
    compare,
    describe,
)
from tvbf.config import get_settings
from tvbf.db import SessionLocal
from tvbf.logging_config import configure_logging

log = logging.getLogger(__name__)


def dumps(snapshot: dict[str, Any]) -> str:
    """The one place the artifact's byte format is decided.

    `sort_keys` plus the service's ordering makes two runs of an unchanged
    database byte-identical, which is what "diffable" has to mean if a baseline
    is going to live in git.
    """
    return json.dumps(snapshot, indent=2, sort_keys=True) + "\n"


async def run(args: argparse.Namespace) -> int:
    """The whole job, minus argument parsing. Returns the process exit code.

    Split out from `main` so tests can await it on their own event loop —
    `main`'s `asyncio.run` would otherwise rebuild the loop under the shared
    engine's pooled connections.
    """
    async with SessionLocal() as s:
        if args.mode == "capture":
            # stdout carries the artifact and nothing else.
            sys.stdout.write(dumps(await build_snapshot(s, spine=args.spine)))
            return 0

        if not args.baseline:
            log.error("verify needs --baseline <path>, or --baseline - to read stdin")
            return 1
        if args.baseline == "-":
            baseline = json.load(sys.stdin)
        else:
            with open(args.baseline) as fh:
                baseline = json.load(fh)

        current = await build_snapshot(s, spine=args.spine)
        lines = await describe(s, compare(baseline, current), spine=args.spine)

    source = "stdin" if args.baseline == "-" else args.baseline
    if lines:
        log.error("reconciliation FAILED against %s: %d discrepancies", source, len(lines))
        for line in lines:
            log.error("  %s", line)
        return 1

    log.info("reconciliation passed against %s: nothing moved", source)
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m tvbf.jobs.reconcile")
    parser.add_argument("mode", choices=("capture", "verify"))
    parser.add_argument(
        "--baseline",
        help="baseline JSON to diff against (verify only); '-' reads stdin",
    )
    parser.add_argument(
        "--spine",
        choices=sorted(SPINES),
        default=DEFAULT_SPINE,
        help="which catalog schema resolves episode -> show",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
