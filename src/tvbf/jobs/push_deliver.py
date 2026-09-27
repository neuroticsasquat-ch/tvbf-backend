"""The daily push delivery, as a Coolify scheduled task (NEU-1489, project spec §5.2).

    python -m tvbf.jobs.push_deliver

The fifth scheduled task, and the same contract as the three run-row passes
beside it: **0 = the run succeeded, 1 = it failed**, with its own
`HEALTHCHECK_PUSH_URL` deadman for the case Coolify cannot report, which is the
task never running at all. Poll a run through the unfiltered
`GET /admin/ingest/{run_id}`; `shows_processed` / `shows_failed` carry the
sent / failed counts.

**Scheduled daily at 13:00 UTC, after the catalog delta and the airdate
reconcile** — it reads the events the delta detected and the dates the
reconcile corrected. Nothing here can enforce that order, and the cost of
getting it wrong is a notification a day late.

**Refuses to start without VAPID keys**: exit 1, logged and pinged `/fail`,
without writing a run row. A job that cannot sign a single push would otherwise
log every candidate as a failure — or, if nobody is subscribed yet, succeed
silently for as long as the keys stay missing.

**Exit 1 only when every send failed** (step 6); an individual device failing is
the delivery log's business, not Coolify's.

The shared mechanics live in `tvbf.jobs.scheduled`; the pass in
`tvbf.push.delivery`.
"""

import logging
import sys

from tvbf.config import Settings
from tvbf.jobs.scheduled import ping, run_scheduled_delta, scheduled_main
from tvbf.push.delivery import run_push_delivery_job

log = logging.getLogger(__name__)

KIND = "push_deliver"
NAME = "push delivery"


async def run_push_daily(settings: Settings) -> bool:
    if not settings.vapid_configured:
        log.error(
            "%s refused: VAPID_PRIVATE_KEY, VAPID_PUBLIC_KEY and VAPID_SUBJECT must all be set",
            NAME,
        )
        await ping(settings.healthcheck_push_url, "/fail")
        return False
    return await run_scheduled_delta(
        settings=settings,
        kind=KIND,
        worker=run_push_delivery_job,
        healthcheck_url=settings.healthcheck_push_url,
        name=NAME,
    )


def main() -> int:
    return scheduled_main(
        runner=run_push_daily,
        healthcheck_url=lambda s: s.healthcheck_push_url,
        name=NAME,
    )


if __name__ == "__main__":
    sys.exit(main())
