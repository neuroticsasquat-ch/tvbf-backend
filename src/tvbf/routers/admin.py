import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.catalog import models as m
from tvbf.catalog.runs import create_run, finalize_run, find_live_run
from tvbf.config import Settings, get_settings
from tvbf.db import SessionLocal
from tvbf.deps import get_session, require_admin
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

    A background task rather than a CLI, unlike `catalog_copy` and
    `tmdb_enrichment`: those are minutes-to-hours one-shots with nothing to
    poll, where this is a multi-hour pass whose progress an operator watches.
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
