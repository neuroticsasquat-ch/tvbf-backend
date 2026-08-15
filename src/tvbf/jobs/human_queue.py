"""The human matching queue, as a CLI (NEU-1044).

    python -m tvbf.jobs.human_queue list [--no-candidates]
    python -m tvbf.jobs.human_queue confirm <show_id> <tmdb_id>
    python -m tvbf.jobs.human_queue reject <show_id>

A CLI rather than an admin UI because the residue is six rows: measured in
production on 2026-08-10, 559 of 565 user-touched shows matched on an exact id.
`tvbf.tmdb.human_queue` holds the query and the three resolutions; this is
argparse over them.

**`list` writes JSON to stdout and nothing else**, for the same reason
`tvbf.jobs.reconcile` does: `docs/` is not in the production image and a Coolify
container is replaced on every deploy, so the report has to travel over
`ssh 'docker exec ...'` rather than be written to a path inside one. Logs go to
stderr.

**Exit codes: 0 = the command did what it says, 1 = it refused or raised.** `list`
exits 0 whether or not the queue is empty — it is a report, and *empty* is the
thing the cutover gate reads out of it, not a failure. `confirm` and `reject`
exit 1 on every refusal, so a scripted resolution cannot mistake "another row
already holds that id" for success.

`--no-candidates` skips every upstream call, which makes `list` a pure database
query needing no TMDB credential — the fast way to ask whether the queue is
empty.
"""

import argparse
import asyncio
import json
import logging
import sys

from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.config import get_settings
from tvbf.db import SessionLocal
from tvbf.logging_config import configure_logging
from tvbf.tmdb.client import TMDBClient
from tvbf.tmdb.human_queue import (
    QueueError,
    annotate,
    build_queue,
    confirm,
    reject,
    unmirrored_user_touched_shows,
)

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


async def _list(db: AsyncSession, *, candidates: bool) -> int:
    rows = await build_queue(db)
    if candidates and rows:
        async with _tmdb_client() as client:
            report = await annotate(client, rows)
    else:
        report = await annotate(None, rows)

    # The artifact, and nothing else, on stdout.
    sys.stdout.write(json.dumps(report, indent=2) + "\n")

    # Before the counts, because it is the one way this report can be wrong
    # rather than merely long: these shows are invisible to the queue and would
    # otherwise let it read empty.
    unmirrored = await unmirrored_user_touched_shows(db)
    if unmirrored:
        log.error(
            "%d user-touched show(s) have no catalog.show row and are NOT in the report "
            "above; the copy that would have created them went with the tvmaze schema "
            "in NEU-1051, so these need authoring by hand: %s",
            len(unmirrored),
            ", ".join(str(show_id) for show_id in unmirrored),
        )

    at_risk = sum(1 for row in rows if row.carries_user_data)
    if not rows:
        log.info("human matching queue is empty — every user-touched show has a verified mapping")
    else:
        log.info(
            "%d show(s) awaiting a human verdict, %d carrying watch or rating history",
            len(rows),
            at_risk,
        )
    return 0


async def run(args: argparse.Namespace) -> int:
    """The whole job, minus argument parsing. Returns the process exit code.

    Split out from `main` so tests can await it on their own event loop —
    `main`'s `asyncio.run` would otherwise rebuild the loop under the shared
    engine's pooled connections.
    """
    async with SessionLocal() as db:
        try:
            if args.mode == "list":
                return await _list(db, candidates=args.candidates)
            if args.mode == "confirm":
                log.info("%s", await confirm(db, args.show_id, args.tmdb_id))
            else:
                log.info("%s", await reject(db, args.show_id))
        except QueueError as exc:
            log.error("refused: %s", exc)
            return 1
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m tvbf.jobs.human_queue")
    modes = parser.add_subparsers(dest="mode", required=True)

    listing = modes.add_parser("list", help="report every show awaiting a human verdict (JSON)")
    listing.add_argument(
        "--no-candidates",
        dest="candidates",
        action="store_false",
        help="skip the TMDB lookups; needs no credential",
    )

    confirming = modes.add_parser("confirm", help="record that a show IS a TMDB series")
    confirming.add_argument("show_id", type=int)
    confirming.add_argument("tmdb_id", type=int)

    rejecting = modes.add_parser("reject", help="record that TMDB has no counterpart for a show")
    rejecting.add_argument("show_id", type=int)

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level)
    try:
        return asyncio.run(run(args))
    except Exception:
        log.exception("human matching queue command failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
