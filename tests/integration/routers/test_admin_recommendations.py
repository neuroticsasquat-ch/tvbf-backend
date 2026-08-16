"""`POST /admin/recommendations` — the manual trigger for the weekly pass (NEU-1110).

The route has no status route to poll and writes no run row, so almost
everything worth pinning is at the seam: what it refuses **before** answering
202, and that what it spawns goes through `run_pass_if_free` — the shared
advisory lock — rather than through `run_pass` directly. A trigger that bypassed
the lock would have a manual run fired minutes before the cron read the same
stale payload hash and spend a second call on every user.

No test ever reaches DeepInfra: the background coroutine is captured instead of
scheduled, and `run_pass_if_free` is stubbed where the test awaits it.
"""

import asyncio
import uuid

import httpx
import pytest
from fastapi import HTTPException
from httpx import ASGITransport

from tvbf.config import get_settings
from tvbf.main import app
from tvbf.routers import admin as admin_router

MODEL = "deepseek-ai/DeepSeek-V4-Pro-0813"


@pytest.fixture
def provider(monkeypatch):
    """A configured provider, and no cached `Settings` left behind.

    `get_settings` is `lru_cache`d, so a test that sets the env without clearing
    it either reads a stale value or hands one to whatever runs next.
    """
    monkeypatch.setenv("RECOMMENDATION_MODEL", MODEL)
    monkeypatch.setenv("DEEPINFRA_API_KEY", "test-key")
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


@pytest.fixture
def spawned(monkeypatch):
    """Capture the pass instead of scheduling it, and schedule everything else.

    `admin_router.asyncio` *is* the `asyncio` module, so a blanket replacement of
    `create_task` reaches anyio and httpx too — which the tests that drive the
    route over real HTTP would then break on. Only this route's own background
    coroutine is intercepted; the coroutine is left un-awaited unless the test
    awaits it itself.
    """
    captured: list = []
    real = asyncio.create_task

    async def _nothing() -> None:
        return None

    def _capture(coro, *args, **kwargs):
        if getattr(coro, "__name__", None) == "_background_weekly_recommendations":
            captured.append(coro)
            # A real task all the same: the route keeps a strong reference to
            # what it spawned and hangs a done callback off it.
            return real(_nothing())
        return real(coro, *args, **kwargs)

    monkeypatch.setattr(admin_router.asyncio, "create_task", _capture)
    return captured


@pytest.fixture
async def admin_client(session, monkeypatch):
    """The route over real HTTP, with the admin token and a configured provider.

    The body is optional at the wire level, which is the half calling the
    function directly cannot check.
    """
    monkeypatch.setenv("ADMIN_TOKEN", "shh")
    monkeypatch.setenv("RECOMMENDATION_MODEL", MODEL)
    monkeypatch.setenv("DEEPINFRA_API_KEY", "test-key")
    get_settings.cache_clear()
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        client.headers["Authorization"] = "Bearer shh"
        yield client
    get_settings.cache_clear()


async def test_post_requires_admin_token(admin_client, spawned):
    resp = await admin_client.post("/admin/recommendations", headers={"Authorization": ""})

    assert resp.status_code in (401, 403)
    assert not spawned


async def test_over_http_the_body_is_optional(admin_client, spawned, make_user):
    """No body at all, an empty object, and a `user_id` all answer 202 — the
    wire-level half of the contract, which calling the route function cannot
    check."""
    user = await make_user()

    for kwargs in ({}, {"json": {}}, {"json": {"user_id": str(user.id)}}):
        resp = await admin_client.post("/admin/recommendations", **kwargs)
        assert resp.status_code == 202, resp.text
        assert uuid.UUID(resp.json()["run_id"])

    assert len(spawned) == 3
    for coro in spawned:
        coro.close()


async def test_it_answers_202_with_a_correlation_id(session, provider, spawned):
    """202 + run_id, matching every other trigger here — but the id names log
    lines rather than a row, because this job writes no run row at all."""
    out = await admin_router.trigger_weekly_recommendations(
        body=None, settings=provider, session=session
    )

    assert uuid.UUID(out["run_id"])
    assert spawned, "the route must spawn the pass in the background"
    spawned[0].close()


async def test_no_body_runs_the_same_pass_the_schedule_runs(
    session, provider, spawned, monkeypatch
):
    """Over everybody, and through `run_pass_if_free` rather than `run_pass` —
    the shared lock is what makes a trigger fired during the cron a no-op instead
    of a second call per user. `None` back is that no-op, and the task survives
    it."""
    calls: list = []

    async def _fake(settings, *, user_id=None):
        calls.append(user_id)
        return None  # what a held lock reports

    monkeypatch.setattr(admin_router, "run_pass_if_free", _fake)

    await admin_router.trigger_weekly_recommendations(body=None, settings=provider, session=session)
    await spawned[0]

    assert calls == [None]


async def test_a_user_id_narrows_the_run_to_that_account(
    session, provider, spawned, make_user, monkeypatch
):
    """The reason the endpoint exists: one account, after a prompt change,
    without waiting for Sunday."""
    from tvbf.app.schemas import RecommendationsRunRequest

    user = await make_user()
    calls: list = []

    async def _fake(settings, *, user_id=None):
        calls.append(user_id)
        return None

    monkeypatch.setattr(admin_router, "run_pass_if_free", _fake)

    await admin_router.trigger_weekly_recommendations(
        body=RecommendationsRunRequest(user_id=user.id), settings=provider, session=session
    )
    await spawned[0]

    assert calls == [user.id]


async def test_a_crashing_pass_is_logged_rather_than_raised(
    session, provider, spawned, monkeypatch
):
    """The 202 went out before the task started; there is no row to fail."""

    async def _boom(settings, *, user_id=None):
        raise RuntimeError("provider is on fire")

    monkeypatch.setattr(admin_router, "run_pass_if_free", _boom)

    await admin_router.trigger_weekly_recommendations(body=None, settings=provider, session=session)
    await spawned[0]  # must not raise


async def test_an_unknown_user_is_refused_before_anything_is_spawned(session, provider, spawned):
    """Otherwise it is a 202 followed by silence in a log nobody is watching."""
    from tvbf.app.schemas import RecommendationsRunRequest

    with pytest.raises(HTTPException) as ei:
        await admin_router.trigger_weekly_recommendations(
            body=RecommendationsRunRequest(user_id=uuid.uuid4()),
            settings=provider,
            session=session,
        )

    assert ei.value.status_code == 404
    assert not spawned


async def test_an_unconfigured_provider_is_refused(session, spawned, monkeypatch):
    """Same reasoning as the pass checking before it compiles anything, one
    layer up: the operator finds out now rather than never."""
    monkeypatch.delenv("RECOMMENDATION_MODEL", raising=False)
    monkeypatch.delenv("DEEPINFRA_API_KEY", raising=False)
    get_settings.cache_clear()
    try:
        with pytest.raises(HTTPException) as ei:
            await admin_router.trigger_weekly_recommendations(
                body=None, settings=get_settings(), session=session
            )
    finally:
        get_settings.cache_clear()

    assert ei.value.status_code == 503
    assert not spawned
