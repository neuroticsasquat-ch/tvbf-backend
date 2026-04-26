"""Lifespan startup coverage tests."""

import pytest

from tvbf.main import app, run_startup_cleanup


@pytest.mark.asyncio
async def test_run_startup_cleanup_is_callable_directly(session):
    """The lifespan body just calls run_startup_cleanup. Calling it directly
    against the test session covers main.run_startup_cleanup itself; the
    lifespan wrapper is exercised by the lifespan test below."""
    count = await run_startup_cleanup(session, stale_after_minutes=15)
    # No stale runs in the empty test DB; should return 0.
    assert count == 0


@pytest.mark.asyncio
async def test_lifespan_executes_startup_cleanup(test_engine):  # noqa: ARG001
    """Enter the @asynccontextmanager lifespan directly. This is what FastAPI's
    server does at boot. Covers main.py:lifespan body."""
    from tvbf.main import lifespan

    async with lifespan(app):
        # Inside the lifespan, startup has run; the yield is reached.
        pass
