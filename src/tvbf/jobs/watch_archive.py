"""Snapshot the watch archive, as a CLI (NEU-1029).

`python -m tvbf.jobs.watch_archive`. A CLI rather than an admin endpoint
because this is a migration-window operation run by hand a handful of times, not
a scheduled job: there is no cursor to advance, nothing to poll, and the whole
snapshot is four `INSERT ... SELECT`s that finish in well under a second at prod
scale (620 tracked shows, 8,499 episode watches, 61 show ratings, 76 episode
ratings). The process *is* the run, so the exit code is the result.

Exit codes: **0 = the archive covers every source row, 1 = it does not.** Re-run
freely; the snapshot is idempotent and append-only.
"""

import asyncio
import logging
import sys

from tvbf.app.services.watch_archive_service import ArchiveSnapshot, snapshot
from tvbf.config import get_settings
from tvbf.db import SessionLocal
from tvbf.logging_config import configure_logging

log = logging.getLogger(__name__)


async def run_snapshot() -> ArchiveSnapshot:
    async with SessionLocal() as s:
        return await snapshot(s)


def main() -> int:
    settings = get_settings()
    configure_logging(settings.log_level)
    try:
        result = asyncio.run(run_snapshot())
    except Exception:
        log.exception("watch archive snapshot failed")
        return 1

    log.info(
        "watch archive complete: %d source rows, %d inserted this run, %d archived total",
        result.source_total,
        result.inserted_total,
        result.archived_total,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
