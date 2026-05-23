import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import sentry_sdk
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.config import get_settings
from tvbf.db import SessionLocal
from tvbf.integrations.linear import LinearClient
from tvbf.routers import (
    admin,
    admin_invites,
    admin_users,
    auth,
    browse,
    connections,
    email_change,
    email_verification,
    feedback,
    friend_engagement,
    health,
    invites_admin,
    me,
    password_reset,
    users,
)
from tvbf.tvmaze.runs import mark_stale_runs_cancelled

if dsn := os.environ.get("SENTRY_DSN"):
    sentry_sdk.init(
        dsn=dsn,
        integrations=[
            FastApiIntegration(),
            SqlalchemyIntegration(),
        ],
        traces_sample_rate=0.1,  # 10% of requests get perf traces
        environment=os.environ.get("ENVIRONMENT", "development"),
        release=os.environ.get("GIT_SHA", "unknown"),
    )


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        force=True,
    )


async def run_startup_cleanup(session: AsyncSession, stale_after_minutes: int) -> int:
    count = await mark_stale_runs_cancelled(session, stale_after_minutes=stale_after_minutes)
    return count


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    async with SessionLocal() as session:
        await run_startup_cleanup(session, stale_after_minutes=settings.ingest_stale_run_minutes)
        await session.commit()

    linear_http: httpx.AsyncClient | None = None
    if settings.linear_feedback_enabled and settings.linear_api_key:
        linear_http = httpx.AsyncClient(timeout=10.0)
        app.state.linear_client = LinearClient(api_key=settings.linear_api_key, http=linear_http)
    else:
        app.state.linear_client = None

    try:
        yield
    finally:
        if linear_http is not None:
            await linear_http.aclose()


def create_app() -> FastAPI:
    settings = get_settings()
    _configure_logging(settings.log_level)
    app = FastAPI(title="tvbf-backend", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "X-CSRF-Token"],
    )
    app.include_router(health.router)
    app.include_router(admin.router)
    app.include_router(admin_users.router)
    app.include_router(admin_invites.router)
    app.include_router(invites_admin.router)
    app.include_router(browse.router)
    app.include_router(auth.router)
    app.include_router(me.router)
    app.include_router(email_verification.router)
    app.include_router(email_change.router)
    app.include_router(feedback.router)
    app.include_router(password_reset.router)
    app.include_router(users.router)
    app.include_router(connections.router)
    app.include_router(friend_engagement.router)
    return app


app = create_app()
