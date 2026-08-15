"""The nightly airdate reconciliation, as a Coolify scheduled task (NEU-1145).

    python -m tvbf.jobs.airdate_reconcile

Same contract as the catalog delta beside it: **0 = the pass ran and succeeded,
1 = it failed**, with a healthchecks.io deadman for the case Coolify cannot
report, which is the task never running at all. The process *is* the run, so
there is no 202 and no status route of its own — poll it through the unfiltered
`GET /admin/ingest/{run_id}`, which filters on nothing precisely so a run of any
kind is readable.

**Its own deadman, not the catalog delta's.** The rule that put the delta on a
check of its own binds here for the same reason: one check fed by two tasks lets
either keep it alive while the other quietly stops. Folding this into the delta
would buy guaranteed ordering after it, which the write path makes unnecessary —
a delta can never clobber a correction, because it re-derives one from the raw
value it is writing.

The shared mechanics live in `tvbf.jobs.scheduled`; what varies is here.
"""

import sys

from tvbf.airdates.reconcile import run_airdate_reconcile_job
from tvbf.config import Settings
from tvbf.jobs.scheduled import run_scheduled_delta, scheduled_main

KIND = "airdate_reconcile"
NAME = "airdate reconciliation"


async def run_airdate_daily(settings: Settings) -> bool:
    return await run_scheduled_delta(
        settings=settings,
        kind=KIND,
        worker=run_airdate_reconcile_job,
        healthcheck_url=settings.healthcheck_airdate_url,
        name=NAME,
    )


def main() -> int:
    return scheduled_main(
        runner=run_airdate_daily,
        healthcheck_url=lambda s: s.healthcheck_airdate_url,
        name=NAME,
    )


if __name__ == "__main__":
    sys.exit(main())
