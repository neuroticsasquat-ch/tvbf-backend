"""The daily `/trending/tv/week` snapshot, as a Coolify scheduled task (NEU-1055).

    python -m tvbf.jobs.trending_snapshot

Same contract as the two scheduled passes beside it: **0 = the snapshot ran and
succeeded, 1 = it failed**, with a healthchecks.io deadman for the case Coolify
cannot report, which is the task never running at all. The process *is* the run,
so there is no 202 and no status route of its own — poll it through the
unfiltered `GET /admin/ingest/{run_id}`, which filters on nothing precisely so a
run of any kind is readable.

**Its own deadman, not the catalog delta's** — the rule that gave the delta and
the airdate pass a check each binds here identically: one check fed by two tasks
lets either keep it alive while the other quietly stops. This one is the least
forgiving of the three about being wrong, because a stopped schedule does not
look broken from the outside: the snapshot simply ages, and NEU-1056's seven-day
cutoff turns the section off a week later with no error anywhere.

**It does not need to run after the catalog delta**, unlike the airdate
reconciliation. A trending entry TMDB created this morning is not mirrored until
tonight's delta and is dropped from today's snapshot either way; ordering the
two would buy one day's coverage of a case measured at zero occurrences, and
would couple a fourth task to a third.

The shared mechanics live in `tvbf.jobs.scheduled`; what varies is here.
"""

import sys

from tvbf.config import Settings
from tvbf.jobs.scheduled import run_scheduled_delta, scheduled_main
from tvbf.tmdb.trending import run_trending_snapshot_job

KIND = "trending_snapshot"
NAME = "trending snapshot"


async def run_trending_daily(settings: Settings) -> bool:
    return await run_scheduled_delta(
        settings=settings,
        kind=KIND,
        worker=run_trending_snapshot_job,
        healthcheck_url=settings.healthcheck_trending_url,
        name=NAME,
    )


def main() -> int:
    return scheduled_main(
        runner=run_trending_daily,
        healthcheck_url=lambda s: s.healthcheck_trending_url,
        name=NAME,
    )


if __name__ == "__main__":
    sys.exit(main())
