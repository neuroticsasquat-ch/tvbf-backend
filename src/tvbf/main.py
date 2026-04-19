from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.config import get_settings
from tvbf.db import SessionLocal
from tvbf.routers import admin, health
from tvbf.tvmaze.runs import mark_stale_runs_cancelled


async def run_startup_cleanup(session: AsyncSession, stale_after_minutes: int) -> int:
    count = await mark_stale_runs_cancelled(session, stale_after_minutes=stale_after_minutes)
    return count


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    async with SessionLocal() as session:
        await run_startup_cleanup(session, stale_after_minutes=settings.ingest_stale_run_minutes)
        await session.commit()
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="tvbf-backend", lifespan=lifespan)
    app.include_router(health.router)
    app.include_router(admin.router)
    return app


app = create_app()
