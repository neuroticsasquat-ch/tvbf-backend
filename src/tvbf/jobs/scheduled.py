"""The shape every run-row-backed Coolify-scheduled job shares (NEU-1008, NEU-1035).

Four jobs run on a Coolify schedule today and they do not all fit this shape.
Three do — the TMDB catalog delta, the airdate reconciliation (NEU-1145) and
the daily trending snapshot (NEU-1055) — differing only in which run kind they
take, which body they await and which deadman they feed; the TV Maze daily was
the first and NEU-1050 retired it. The weekly recommendations pass (NEU-1109,
NEU-1111) is the exception and calls `ping` alone: it deliberately writes no
run row (`user_recommendation_set` is already its per-user run record), so the
guard, the row and the terminal-status read here have nothing to act on. What
it does keep is the *rules* below, which is the reason they are written down
rather than merely implemented:

- **The exit code is the result.** Coolify notifies on a task that fails, so 0
  must mean the delta actually ran and succeeded.
- **The work is awaited, never spawned.** `create_task` would exit 0
  immediately, every day, forever — a silent no-op that looks healthy.
- **A deadman covers what Coolify cannot see**, which is the task never running
  at all: suspended and forgotten, container down, scheduler broken.

So the shape lives here once, a new run-row-backed job supplies the three things
that genuinely vary, and one that has no run row takes `ping` and the rules.
"""

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import select

from tvbf.catalog import models as m
from tvbf.catalog.runs import create_run, find_live_run
from tvbf.config import Settings, get_settings
from tvbf.db import SessionLocal
from tvbf.logging_config import configure_logging

log = logging.getLogger(__name__)

Worker = Callable[[UUID, Settings], Coroutine[Any, Any, None]]
Runner = Callable[[Settings], Coroutine[Any, Any, bool]]


async def ping(base_url: str | None, suffix: str = "") -> None:
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


async def run_scheduled_delta(
    *,
    settings: Settings,
    kind: str,
    worker: Worker,
    healthcheck_url: str | None,
    name: str,
) -> bool:
    """Run one delta of `kind` to completion. True iff it finished `succeeded`.

    The in-flight guard is per kind, matching the admin trigger for the same
    kind exactly. It is *not* a concurrency check: another job running at the
    same time is fine and expected, because the request budget is shared across
    processes (ADR-0006), so the two simply go slower rather than doubling the
    rate against upstream.
    """
    await ping(healthcheck_url, "/start")

    async with SessionLocal() as s:
        live = await find_live_run(
            s, kind=kind, stale_after_minutes=settings.ingest_stale_run_minutes
        )
    if live is not None:
        # Someone triggered this by hand minutes before the schedule fired.
        # Exit 0 — this task did nothing wrong, and failing would have Coolify
        # notify on a benign condition.
        #
        # Deliberately no success ping. The admin trigger pings nothing, so a
        # success ping here would report a run whose outcome we never learn: if
        # that hand-triggered run later failed, Coolify would not see it (this
        # process exited 0) and the deadman would already be fed. Staying silent
        # leaves the check in its started state, so the grace period expires and
        # someone looks — a spurious alert on a rare day, in exchange for never
        # swallowing a failed one.
        log.info("a %s run is already in flight (%s); nothing to do", kind, live.id)
        return True

    async with SessionLocal() as s:
        run_id = await create_run(s, kind=kind)
        await s.commit()

    # Awaited, never `create_task` — see the module docstring.
    await worker(run_id, settings)

    status = await _terminal_status(run_id)
    if status != "succeeded":
        log.error("%s run %s ended %s", name, run_id, status)
        await ping(healthcheck_url, "/fail")
        return False

    log.info("%s run %s succeeded", name, run_id)
    await ping(healthcheck_url)
    return True


def scheduled_main(
    *,
    runner: Runner,
    healthcheck_url: Callable[[Settings], str | None],
    name: str,
) -> int:
    """The `main()` every scheduled entrypoint returns from. 0 = ran and succeeded.

    `healthcheck_url` is a callable rather than a string because settings are
    only loaded here — and the ping on this path matters: `runner` finalizes its
    own run, so reaching the handler means the failure was *outside* it (the
    guard query, the run insert, the status read), and none of those leave a
    `failed` row to speak for them.
    """
    settings = get_settings()
    configure_logging(settings.log_level)
    try:
        ok = asyncio.run(runner(settings))
    except Exception:
        log.exception("%s job crashed", name)
        asyncio.run(ping(healthcheck_url(settings), "/fail"))
        return 1
    return 0 if ok else 1
