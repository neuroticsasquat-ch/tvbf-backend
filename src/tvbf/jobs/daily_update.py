"""The daily TV Maze delta, as a Coolify scheduled task.

`python -m tvbf.jobs.daily_update`. Replaces `.github/workflows/daily-update.yml`,
which POSTed `/admin/update` with a repo secret and then polled
`/admin/ingest/{run_id}` every 30s for up to three hours to recover a result the
`202` had thrown away (NEU-1008).

Exit codes are the contract Coolify reads: **0 = the daily ran and succeeded,
1 = it failed.** Coolify notifies on a failed task; a healthchecks.io deadman
covers the case Coolify cannot see, which is the task never running at all.
"""

import asyncio
import logging
import sys
from uuid import UUID

import httpx
from sqlalchemy import select

from tvbf.config import Settings, get_settings
from tvbf.db import SessionLocal
from tvbf.logging_config import configure_logging
from tvbf.tvmaze import models as m
from tvbf.tvmaze.runs import create_run, find_live_run
from tvbf.tvmaze.update import run_update_job

log = logging.getLogger(__name__)

KIND = "update"


async def _ping(base_url: str | None, suffix: str = "") -> None:
    """Best-effort healthchecks.io ping.

    A no-op when unset, and a failure is logged and swallowed so it can never
    change the job's own outcome. A ping that cannot get out is itself what the
    deadman alerts on, so there is nothing to gain by failing the run over it.
    """
    if not base_url:
        return
    url = base_url.rstrip("/") + suffix
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(url)
    except Exception:
        log.warning("healthcheck ping to %s failed", url, exc_info=True)


async def _terminal_status(run_id: UUID) -> str:
    async with SessionLocal() as s:
        return (
            await s.execute(select(m.IngestRun.status).where(m.IngestRun.id == run_id))
        ).scalar_one()


async def run_daily(settings: Settings) -> bool:
    """Run one daily delta to completion. True iff the run finished `succeeded`.

    The guard is per kind, matching `POST /admin/update` exactly. It is *not* a
    concurrency check: an in-app backfill running at the same time is fine and
    expected, because the request budget is shared across processes (ADR-0006),
    so the two simply go slower rather than doubling the rate against upstream.
    """
    await _ping(settings.healthcheck_daily_url, "/start")

    async with SessionLocal() as s:
        live = await find_live_run(
            s, kind=KIND, stale_after_minutes=settings.ingest_stale_run_minutes
        )
    if live is not None:
        # Someone triggered a daily by hand minutes before the schedule fired.
        # Exit 0 — this task did nothing wrong, and failing would have Coolify
        # notify on a benign condition.
        #
        # Deliberately no success ping. `POST /admin/update` pings nothing, so a
        # success ping here would report a run whose outcome we never learn: if
        # that hand-triggered run later failed, Coolify would not see it (this
        # process exited 0) and the deadman would already be fed. Staying silent
        # leaves the check in its started state, so the grace period expires and
        # someone looks — a spurious alert on a rare day, in exchange for never
        # swallowing a failed one.
        log.info("an update run is already in flight (%s); nothing to do", live.id)
        return True

    async with SessionLocal() as s:
        run_id = await create_run(s, kind=KIND)
        await s.commit()

    # Awaited, never `create_task`: the whole point of a CLI is that the process
    # outlives the work and its exit code reflects it. Spawning would exit 0
    # immediately, every day, forever — a silent no-op that looks healthy.
    await run_update_job(run_id, settings)

    status = await _terminal_status(run_id)
    if status != "succeeded":
        log.error("daily update run %s ended %s", run_id, status)
        await _ping(settings.healthcheck_daily_url, "/fail")
        return False

    log.info("daily update run %s succeeded", run_id)
    await _ping(settings.healthcheck_daily_url)
    return True


def main() -> int:
    settings = get_settings()
    configure_logging(settings.log_level)
    try:
        ok = asyncio.run(run_daily(settings))
    except Exception:
        # `run_update_job` finalizes its own run, so reaching here means the
        # failure was outside it — the guard query, the run insert, or the
        # status read. None of those leave a `failed` row to speak for them.
        log.exception("daily update job crashed")
        asyncio.run(_ping(settings.healthcheck_daily_url, "/fail"))
        return 1
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
