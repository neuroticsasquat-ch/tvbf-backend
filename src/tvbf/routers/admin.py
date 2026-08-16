import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.repos import user_repo
from tvbf.app.schemas import RecommendationsRunRequest
from tvbf.catalog import models as m
from tvbf.catalog.runs import create_run, finalize_run, find_live_run
from tvbf.config import Settings, get_settings
from tvbf.db import SessionLocal
from tvbf.deps import get_session, require_admin
from tvbf.jobs.weekly_recommendations import run_pass_if_free
from tvbf.tmdb.client import TMDBClient
from tvbf.tmdb.ingest import run_catalog_ingest
from tvbf.tmdb.update import run_catalog_update_job

log = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


def _session_factory():
    return SessionLocal()


BackgroundWorker = Callable[[UUID, Settings], Coroutine[Any, Any, None]]


async def _start_run(
    session: AsyncSession, settings: Settings, kind: str, worker: BackgroundWorker
) -> dict[str, str]:
    """Guard, create the run row, spawn its worker, and return the run id.

    One helper for every trigger route so `kind` is named exactly once per
    route. Guarding and creating as two separate calls reads fine and
    type-checks, but a copy-paste that guards one kind while creating another
    silently disables the guard — the failure this exists to prevent.

    Refuses with 409 when a run of `kind` is already in flight. Two runs of
    one kind corrupt nothing — the upserts are idempotent — but the upstream
    request budget is shared (NEU-955, ADR-0006), so they split one allowance
    and each simply crawls. On a multi-hour pass an accidental double-POST
    therefore doubles the wall-clock.

    Scoped per kind, so a stuck pass never blocks an urgent delta. The
    `tvbf.jobs.catalog_update` CLI applies the same per-kind check, which is
    safe precisely because the budget spans processes: a delta running
    alongside an in-app pass is slower, not over the cap.

    Advisory, not atomic: the guard, the insert and the commit are three
    statements, so two requests interleaving between the SELECT and the
    COMMIT would both pass. The threat model is an operator's stray second
    POST, which is sequential. A partial unique index on (kind, status) would
    be the fix if that ever stops being true.
    """
    live = await find_live_run(
        session, kind=kind, stale_after_minutes=settings.ingest_stale_run_minutes
    )
    if live is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"a {kind} run is already in flight: {live.id}",
        )
    run_id = await create_run(session, kind=kind)
    await session.commit()
    asyncio.create_task(worker(run_id, settings))
    return {"run_id": str(run_id)}


async def _background_catalog_ingest(run_id: UUID, settings: Settings) -> None:
    """The TMDB full-catalog pass (NEU-1034).

    A background task rather than a CLI, unlike `tmdb_enrichment` (and
    `catalog_copy`, until NEU-1051 deleted it): those are minutes-to-hours
    one-shots with nothing to poll, where this is a multi-hour pass whose
    progress an operator watches.
    """
    try:
        async with TMDBClient(
            base_url=settings.tmdb_base_url,
            read_access_token=settings.tmdb_read_access_token,
            rate_calls=settings.tmdb_rate_limit_requests,
            rate_window=settings.tmdb_rate_limit_window_seconds,
            retry_max_attempts=settings.tmdb_retry_max_attempts,
        ) as client:
            await run_catalog_ingest(
                session_factory=_session_factory,
                client=client,
                run_id=run_id,
                failure_threshold=settings.ingest_consecutive_failure_threshold,
            )
    except Exception as e:
        log.exception("background catalog ingest crashed")
        async with SessionLocal() as s:
            await finalize_run(s, run_id, status="failed", error=str(e))
            await s.commit()


def _serialize_run(row: m.IngestRun) -> dict:
    return {
        "id": str(row.id),
        "kind": row.kind,
        "status": row.status,
        "started_at": row.started_at.isoformat(),
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "shows_processed": row.shows_processed,
        "shows_failed": row.shows_failed,
        "last_update_cursor": row.last_update_cursor,
        "error": row.error,
    }


@router.get("/ingest/{run_id}")
async def get_run_status(run_id: UUID, session: AsyncSession = Depends(get_session)) -> dict:
    """Any run row, whatever its kind — the one status route that filters nothing.

    Its path outlived the `POST /admin/ingest` it was named for (NEU-1050). It
    stays because it is what a run with no status route of its own is polled
    through — `catalog_update` today — and because run ids from the retired
    TV Maze passes stay readable through it: NEU-1051 moved the table into
    `catalog` with `SET SCHEMA`, so those rows outlived the schema they were
    written in. Renaming it would break both for nothing.
    """
    row = (
        await session.execute(select(m.IngestRun).where(m.IngestRun.id == run_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="run not found")
    return _serialize_run(row)


@router.post("/catalog-ingest", status_code=status.HTTP_202_ACCEPTED)
async def trigger_catalog_ingest(
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    return await _start_run(session, settings, "catalog_initial", _background_catalog_ingest)


@router.get("/catalog-ingest/{run_id}")
async def get_catalog_ingest_status(
    run_id: UUID, session: AsyncSession = Depends(get_session)
) -> dict:
    row = (
        await session.execute(select(m.IngestRun).where(m.IngestRun.id == run_id))
    ).scalar_one_or_none()
    if row is None or row.kind != "catalog_initial":
        raise HTTPException(status_code=404, detail="run not found")
    return _serialize_run(row)


@router.post("/catalog-update", status_code=status.HTTP_202_ACCEPTED)
async def trigger_catalog_update(
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    """Trigger one TMDB catalog delta by hand (NEU-1035).

    The manual counterpart to the `tvbf.jobs.catalog_update` scheduled task. No
    status route of its own, because a delta is minutes rather than hours —
    poll the unfiltered `GET /admin/ingest/{run_id}` if you want its progress.
    """
    return await _start_run(session, settings, "catalog_update", run_catalog_update_job)


_background_tasks: set[asyncio.Task[None]] = set()
"""Strong references to the passes spawned by `POST /admin/recommendations`.

`asyncio` keeps only a weak reference to a running task, so one nothing else
holds can be garbage-collected mid-run. `_start_run`'s workers survive that
badly but visibly — a vanished catalog pass leaves a `running` `ingest_run` row
for the lifespan hook's stale-run cleanup to find. This job writes no row at all
(project spec §10), so the only trace would be a log line that stopped, which is
indistinguishable from a pass still working. Discarding on completion is what
keeps this from growing.
"""


async def _background_weekly_recommendations(
    run_id: UUID, settings: Settings, user_id: UUID | None
) -> None:
    """One recommendations pass, under the same lock the schedule takes.

    Nothing is written about the run itself: there is no `ingest_run` row for
    this job and no run table of any kind (project spec §10), because
    `app.user_recommendation_set` already *is* the per-user run record. So
    `run_id` is a correlation id and nothing more — it is what ties these log
    lines to the 202 the operator got back, and it is why the route says nothing
    about polling.

    Every failure is swallowed into the log for the same reason: the response
    went out before this started, and there is no row to mark failed.
    """
    scope = f"user {user_id}" if user_id is not None else "every user"
    log.info("admin recommendations run %s starting over %s", run_id, scope)
    try:
        result = await run_pass_if_free(settings, user_id=user_id)
    except Exception:
        log.exception("admin recommendations run %s crashed", run_id)
        return
    if result is None:
        log.info("admin recommendations run %s: a pass was already running", run_id)
    else:
        log.info("admin recommendations run %s finished: %s", run_id, result)


@router.post("/recommendations", status_code=status.HTTP_202_ACCEPTED)
async def trigger_weekly_recommendations(
    body: RecommendationsRunRequest | None = None,
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    """Run the weekly recommendations pass now, optionally for one user (NEU-1110).

    The manual counterpart to the Sunday Coolify schedule. It exists so a prompt
    change can be tried against one account without waiting for Sunday, which is
    the difference between an iteration loop measured in minutes and one measured
    in weeks.

    `202 + run_id` and a background task, matching every other trigger here even
    though **there is no status route to poll** — the run leaves no row behind
    (see `_background_weekly_recommendations`), so the id is a correlation id for
    the logs. Precedent: the retired `/admin/update` and `/admin/update-people`
    had no status route of their own either.

    No 409 guard, unlike `_start_run`: liveness for this job is
    `pg_try_advisory_lock`, not a run row, and a second trigger is a no-op rather
    than a double spend. Refusing here as well would mean a second liveness
    notion for one job, which is what the missing run table exists to avoid.

    The two refusals are both about the only feedback this endpoint can give.
    A misconfigured provider or an unknown user id would otherwise be a 202
    followed by silence in a log nobody is watching, so both are answered
    synchronously, before the task is spawned.
    """
    user_id = body.user_id if body is not None else None
    if not settings.recommendation_model or not settings.deepinfra_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="RECOMMENDATION_MODEL and DEEPINFRA_API_KEY must both be set",
        )
    if user_id is not None and await user_repo.get_by_id(session, user_id) is None:
        raise HTTPException(status_code=404, detail=f"no user with id {user_id}")

    run_id = uuid4()
    task = asyncio.create_task(_background_weekly_recommendations(run_id, settings, user_id))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return {"run_id": str(run_id)}
