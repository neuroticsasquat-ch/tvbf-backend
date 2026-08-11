"""The daily TV Maze delta, as a Coolify scheduled task.

`python -m tvbf.jobs.daily_update`. Replaces `.github/workflows/daily-update.yml`,
which POSTed `/admin/update` with a repo secret and then polled
`/admin/ingest/{run_id}` every 30s for up to three hours to recover a result the
`202` had thrown away (NEU-1008).

Exit codes are the contract Coolify reads: **0 = the daily ran and succeeded,
1 = it failed.** Coolify notifies on a failed task; a healthchecks.io deadman
covers the case Coolify cannot see, which is the task never running at all.

The mechanics of all of that live in `tvbf.jobs.scheduled`, shared with the TMDB
catalog delta (NEU-1035) — what is left here is this job's three variables.
"""

import sys

from tvbf.config import Settings
from tvbf.jobs.scheduled import run_scheduled_delta, scheduled_main
from tvbf.tvmaze.update import run_update_job

KIND = "update"
NAME = "daily update"


async def run_daily(settings: Settings) -> bool:
    return await run_scheduled_delta(
        settings=settings,
        kind=KIND,
        worker=run_update_job,
        healthcheck_url=settings.healthcheck_daily_url,
        name=NAME,
    )


def main() -> int:
    return scheduled_main(
        runner=run_daily,
        healthcheck_url=lambda s: s.healthcheck_daily_url,
        name=NAME,
    )


if __name__ == "__main__":
    sys.exit(main())
