"""The shape every run-row-backed Coolify-scheduled job shares (NEU-1008, NEU-1035).

Six jobs run on a Coolify schedule today and they do not all fit this shape.
Five do — the TMDB catalog delta, the airdate reconciliation (NEU-1145), the
daily trending snapshot (NEU-1055) and the two push delivery tasks (NEU-1540) —
differing only in which run kind they take, which body they await and which
deadman they feed; the TV Maze daily was
the first and NEU-1050 retired it. The weekly recommendations pass (NEU-1109,
NEU-1111) is the exception and calls `ping_up` / `ping_down` / `report_down`
alone: it deliberately writes no run row (`user_recommendation_set` is already
its per-user run record), so the guard, the row and the terminal-status read
here have nothing to act on. What it does keep is the *rules* below, which is
the reason they are written down rather than merely implemented:

- **The exit code is the result.** Coolify notifies on a task that fails, so 0
  must mean the delta actually ran and succeeded.
- **The work is awaited, never spawned.** `create_task` would exit 0
  immediately, every day, forever — a silent no-op that looks healthy.
- **A deadman covers what Coolify cannot see**, which is the task never running
  at all: suspended and forgotten, container down, scheduler broken.

So the shape lives here once, a new run-row-backed job supplies the three things
that genuinely vary, and one that has no run row takes the pings and the rules.

The deadman is an Uptime Kuma push monitor (NEU-1581): one `status=up` per
success, one `status=down` with a message per failure, and **no start ping** —
Kuma has none. A run that never reports anything is caught the same way a run
that never starts is, by the monitor's interval expiring.

A run that *crashes* is also one error-tracker event (NEU-1622), sent beside
its `status=down` by `report_down` so the two signals cannot drift apart: an
exception that escapes the runner, or a run row finalized `failed` — the
workers catch their own body's crash and finalize the row, so that status is
where most real crashes surface. Every other down only pings: a `cancelled`
row (the startup sweep of a stalled run), a missing setting, failures the job
tolerated. That is the only way a job reaches Bugsink: `init_sentry(job=…)`
keeps log records out of it, because a job logs per failed item and carries on.
"""

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine
from typing import Any
from uuid import UUID

import httpx
import sentry_sdk
from sqlalchemy import select

from tvbf.catalog import models as m
from tvbf.catalog.runs import create_run, find_live_run
from tvbf.config import Settings, get_settings
from tvbf.db import SessionLocal
from tvbf.logging_config import configure_logging
from tvbf.telemetry import init_sentry

log = logging.getLogger(__name__)

Worker = Callable[[UUID, Settings], Coroutine[Any, Any, None]]
Runner = Callable[[Settings], Coroutine[Any, Any, bool]]


# Kuma stores the message whole; a traceback's worth of it helps nobody in an alert.
MAX_PING_MESSAGE = 250


async def _push(url: str | None, params: dict[str, str]) -> None:
    """Best-effort Uptime Kuma push.

    A no-op when unset, and a failure is logged and swallowed so it can never
    change the job's own outcome. A ping that cannot get out is itself what the
    deadman alerts on, so there is nothing to gain by failing the run over it.

    `params` replace any query string already on `url`, so a push URL pasted
    from Kuma with its default `?status=up&msg=OK&ping=` still works.
    """
    if not url:
        return
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.get(httpx.URL(url).copy_with(params=params))
    except Exception:
        log.warning("deadman ping to %s failed", url, exc_info=True)


async def ping_up(url: str | None, *, duration_ms: int) -> None:
    """Report a successful run, and how long it took, to the push monitor."""
    await _push(url, {"status": "up", "msg": "ok", "ping": str(duration_ms)})


async def ping_down(url: str | None, msg: str) -> None:
    """Report a failed run to the push monitor, with `msg` saying why."""
    await _push(url, {"status": "down", "msg": msg[:MAX_PING_MESSAGE]})


async def report_down(
    url: str | None,
    msg: str,
    *,
    exc: BaseException | None = None,
    event: str | None = None,
) -> None:
    """Report a crashed run on both channels: one error-tracker event, then `ping_down`.

    With `exc` the event is that exception and its traceback. Without one it is
    an error-level message, `event` when given: Kuma's `msg` carries run ids and
    counts, and a Bugsink issue is grouped by its text, so a caller whose `msg`
    varies run to run passes a stable `event` and every failure of that kind
    lands on one issue rather than opening a new one each day. The full `msg`
    rides along as `extra.detail`. A no-op on the tracker without `SENTRY_DSN`.
    """
    if exc is not None:
        sentry_sdk.capture_exception(exc)
    else:
        sentry_sdk.capture_message(event or msg, level="error", extras={"detail": msg})
    await ping_down(url, msg)


def failure_message(exc: BaseException) -> str:
    """`ping_down`'s message for a crash: the exception type and its first line."""
    lines = str(exc).strip().splitlines()
    return f"{type(exc).__name__}: {lines[0]}" if lines else type(exc).__name__


def elapsed_ms(started: float) -> int:
    """Milliseconds since `started`, a `time.monotonic()` reading."""
    return round((time.monotonic() - started) * 1000)


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
    started = time.monotonic()

    async with SessionLocal() as s:
        live = await find_live_run(
            s, kind=kind, stale_after_minutes=settings.ingest_stale_run_minutes
        )
    if live is not None:
        # Someone triggered this by hand minutes before the schedule fired.
        # Exit 0 — this task did nothing wrong, and failing would have Coolify
        # notify on a benign condition.
        #
        # Deliberately no ping. The admin trigger pings nothing, so an up ping
        # here would report a run whose outcome we never learn: if that
        # hand-triggered run later failed, Coolify would not see it (this
        # process exited 0) and the deadman would already be fed. Staying silent
        # lets the monitor's interval expire, so someone looks — a spurious
        # alert on a rare day, in exchange for never swallowing a failed one.
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
        msg = f"{name} run {run_id} ended {status}"
        if status == "failed":
            await report_down(healthcheck_url, msg, event=f"{name} run ended failed")
        else:
            await ping_down(healthcheck_url, msg)
        return False

    log.info("%s run %s succeeded", name, run_id)
    await ping_up(healthcheck_url, duration_ms=elapsed_ms(started))
    return True


def scheduled_main(
    *,
    runner: Runner,
    healthcheck_url: Callable[[Settings], str | None],
    name: str,
    job: str,
) -> int:
    """The `main()` every scheduled entrypoint returns from. 0 = ran and succeeded.

    `healthcheck_url` is a callable rather than a string because settings are
    only loaded here — and the ping on this path matters: `runner` finalizes its
    own run, so reaching the handler means the failure was *outside* it (the
    guard query, the run insert, the status read), and none of those leave a
    `failed` row to speak for them.

    `job` is the module's own name (`catalog_update`), the `pipeline_mode` tag
    on its error-tracker events. It is spelled out rather than read off
    `__name__`, which is `__main__` under `python -m`.
    """
    settings = get_settings()
    configure_logging(settings.log_level)
    init_sentry(job=job)
    try:
        ok = asyncio.run(runner(settings))
    except Exception as exc:
        log.exception("%s job crashed", name)
        asyncio.run(report_down(healthcheck_url(settings), failure_message(exc), exc=exc))
        return 1
    finally:
        # The process exits straight after; an event still queued would be lost.
        sentry_sdk.flush()
    return 0 if ok else 1
