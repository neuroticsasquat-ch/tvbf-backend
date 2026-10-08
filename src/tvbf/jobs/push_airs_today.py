"""The daily airs-today push, as a Coolify scheduled task (NEU-1540, spec §1).

    python -m tvbf.jobs.push_airs_today

One of the two push delivery tasks, and the same contract as the run-row passes
beside it: **0 = the run succeeded, 1 = it failed**, with its own
`HEALTHCHECK_PUSH_AIRS_TODAY_URL` deadman for the case Coolify cannot report,
which is the task never running at all. Poll a run through the unfiltered
`GET /admin/ingest/{run_id}`; `shows_processed` / `shows_failed` carry the
sent / failed counts.

**Scheduled daily at 13:00 UTC, after the catalog delta and the airdate
reconcile** — it reads the dates the reconcile corrected. Nothing here can
enforce that order, and the cost of getting it wrong is a notification a day
late. It sends the schedule-derived set only — one push per show airing today —
capped per user by `PUSH_AIRS_TODAY_DAILY_CAP` (default `0`, no cap), and purges
nothing; the events task does that.

**Refuses to start without VAPID keys** (exit 1, `/fail`, no run row) and
**exits 1 only when every send failed** — both `push.delivery.run_delivery_task`'s
rules, shared with `jobs/push_events.py`.
"""

import sys

from tvbf.config import Settings
from tvbf.jobs.scheduled import scheduled_main
from tvbf.push.delivery import AIRS_TODAY, run_delivery_task


async def run_push_airs_today(settings: Settings) -> bool:
    return await run_delivery_task(AIRS_TODAY, settings)


def main() -> int:
    return scheduled_main(
        runner=run_push_airs_today,
        healthcheck_url=AIRS_TODAY.healthcheck_url,
        name=AIRS_TODAY.name,
    )


if __name__ == "__main__":
    sys.exit(main())
