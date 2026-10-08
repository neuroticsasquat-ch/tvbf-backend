"""The daily catalog-event push, as a Coolify scheduled task (NEU-1540, spec §1).

    python -m tvbf.jobs.push_events

One of the two push delivery tasks, and the same contract as the run-row passes
beside it: **0 = the run succeeded, 1 = it failed**, with its own
`HEALTHCHECK_PUSH_EVENTS_URL` deadman for the case Coolify cannot report, which
is the task never running at all. Poll a run through the unfiltered
`GET /admin/ingest/{run_id}`; `shows_processed` / `shows_failed` carry the
sent / failed counts.

**Scheduled daily at 17:00 UTC, after the catalog delta** — it reads the events
the delta detected. Nothing here can enforce that order, and the cost of getting
it wrong is a notification a day late. It sends every fresh, still-current
premiere-set, premiere-moved, ended and revived event, capped per user by
`PUSH_EVENTS_DAILY_CAP` (default `0`, no cap), then purges `catalog.show_event`
and `app.push_delivery` rows older than 90 days.

**Refuses to start without VAPID keys** (exit 1, `/fail`, no run row) and
**exits 1 only when every send failed** — both `push.delivery.run_delivery_task`'s
rules, shared with `jobs/push_airs_today.py`.
"""

import sys

from tvbf.config import Settings
from tvbf.jobs.scheduled import scheduled_main
from tvbf.push.delivery import EVENTS, run_delivery_task


async def run_push_events(settings: Settings) -> bool:
    return await run_delivery_task(EVENTS, settings)


def main() -> int:
    return scheduled_main(
        runner=run_push_events,
        healthcheck_url=EVENTS.healthcheck_url,
        name=EVENTS.name,
    )


if __name__ == "__main__":
    sys.exit(main())
