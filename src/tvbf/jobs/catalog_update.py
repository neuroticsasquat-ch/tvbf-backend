"""The daily TMDB catalog delta, as a Coolify scheduled task (NEU-1035).

`python -m tvbf.jobs.catalog_update`. Same contract as the TV Maze daily it has
now outlived (NEU-1050): **0 = the delta ran and succeeded, 1 = it failed**,
with a healthchecks.io deadman for the case Coolify cannot report, which is the
task never running at all.

It was a second scheduled task rather than an extension of the first because the
two ran against different catalogs on different schedules and had to be able to
fail independently — a TV Maze daily wedged before cutover must not stop the
spine that replaces it from staying current. It is the only one left. The shared
mechanics live in `tvbf.jobs.scheduled`; what varies is here.
"""

import sys

from tvbf.config import Settings
from tvbf.jobs.scheduled import run_scheduled_delta, scheduled_main
from tvbf.tmdb.update import run_catalog_update_job

KIND = "catalog_update"
NAME = "catalog delta"


async def run_catalog_daily(settings: Settings) -> bool:
    return await run_scheduled_delta(
        settings=settings,
        kind=KIND,
        worker=run_catalog_update_job,
        healthcheck_url=settings.healthcheck_catalog_url,
        name=NAME,
    )


def main() -> int:
    return scheduled_main(
        runner=run_catalog_daily,
        healthcheck_url=lambda s: s.healthcheck_catalog_url,
        name=NAME,
    )


if __name__ == "__main__":
    sys.exit(main())
