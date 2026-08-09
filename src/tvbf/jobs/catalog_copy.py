"""Copy `tvmaze.*` into `catalog.*`, as a CLI (NEU-1042).

`python -m tvbf.jobs.catalog_copy`. A CLI rather than an admin endpoint for the
same reason `watch_archive` is one: this is a migration-window operation run by
hand a handful of times, with no cursor to advance and nothing to poll. The
process *is* the run, so the exit code is the result.

Exit codes: **0 = every source row has a row of the same id in `catalog`,
1 = it does not.** Re-run freely — the copy is idempotent, and a re-run fills
only what is missing without rewriting anything already there.

At prod scale this moves 89,025 shows, 85,707 AKAs, 188,134 seasons and
3,530,808 episodes, so unlike the archive it is minutes rather than a second —
episode progress is logged per block of shows.
"""

import asyncio
import logging
import sys

from tvbf.config import get_settings
from tvbf.db import SessionLocal
from tvbf.logging_config import configure_logging
from tvbf.tvmaze.catalog_copy import CopyResult, copy_to_catalog

log = logging.getLogger(__name__)


async def run_copy() -> CopyResult:
    async with SessionLocal() as s:
        result = await copy_to_catalog(s)
        await s.commit()
        return result


def main() -> int:
    settings = get_settings()
    configure_logging(settings.log_level)
    try:
        result = asyncio.run(run_copy())
    except Exception:
        log.exception("catalog copy failed")
        return 1

    for table in result.tables:
        log.info(
            "%s: %d source rows, %d rows present, %d missing",
            table.table,
            table.source_rows,
            table.copied_rows,
            table.missing_rows,
        )
    for table, restart_at in result.sequences.items():
        log.info("%s identity restarts at %d", table, restart_at)

    if not result.complete:
        log.error("catalog copy incomplete — see the missing counts above")
        return 1
    log.info("catalog copy complete: every source row has a catalog row with the same id")
    return 0


if __name__ == "__main__":
    sys.exit(main())
