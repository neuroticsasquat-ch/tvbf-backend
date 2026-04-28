import asyncio
import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.config import Settings, get_settings
from tvbf.db import SessionLocal
from tvbf.deps import get_session, require_admin
from tvbf.tvmaze import models as m
from tvbf.tvmaze.client import TVMazeClient
from tvbf.tvmaze.ingest import run_initial_ingest
from tvbf.tvmaze.runs import create_run, finalize_run
from tvbf.tvmaze.update import run_update

log = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


def _session_factory():
    return SessionLocal()


async def _background_ingest(run_id: UUID, settings: Settings) -> None:
    try:
        async with TVMazeClient(
            base_url=settings.tvmaze_base_url,
            rate_calls=settings.tvmaze_rate_limit_requests,
            rate_window=settings.tvmaze_rate_limit_window_seconds,
            retry_max_attempts=settings.tvmaze_retry_max_attempts,
        ) as client:
            await run_initial_ingest(
                session_factory=_session_factory,
                client=client,
                run_id=run_id,
                failure_threshold=settings.ingest_consecutive_failure_threshold,
            )
    except Exception as e:
        log.exception("background ingest crashed")
        async with SessionLocal() as s:
            await finalize_run(s, run_id, status="failed", error=str(e))
            await s.commit()


async def _background_update(run_id: UUID, settings: Settings) -> None:
    try:
        async with TVMazeClient(
            base_url=settings.tvmaze_base_url,
            rate_calls=settings.tvmaze_rate_limit_requests,
            rate_window=settings.tvmaze_rate_limit_window_seconds,
            retry_max_attempts=settings.tvmaze_retry_max_attempts,
        ) as client:
            await run_update(
                session_factory=_session_factory,
                client=client,
                run_id=run_id,
                failure_threshold=settings.ingest_consecutive_failure_threshold,
            )
    except Exception as e:
        log.exception("background update crashed")
        async with SessionLocal() as s:
            await finalize_run(s, run_id, status="failed", error=str(e))
            await s.commit()


@router.post("/ingest", status_code=status.HTTP_202_ACCEPTED)
async def trigger_ingest(
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    run_id = await create_run(session, kind="initial")
    await session.commit()
    asyncio.create_task(_background_ingest(run_id, settings))
    return {"run_id": str(run_id)}


@router.post("/update", status_code=status.HTTP_202_ACCEPTED)
async def trigger_update(
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    run_id = await create_run(session, kind="update")
    await session.commit()
    asyncio.create_task(_background_update(run_id, settings))
    return {"run_id": str(run_id)}


@router.get("/ingest/{run_id}")
async def get_run_status(run_id: UUID, session: AsyncSession = Depends(get_session)) -> dict:
    row = (
        await session.execute(select(m.IngestRun).where(m.IngestRun.id == run_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="run not found")
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
