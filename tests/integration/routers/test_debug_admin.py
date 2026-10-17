"""POST /admin/debug/raise-test-error — the NEU-1589 test-error trigger."""

import httpx
import pytest
import sentry_sdk
from httpx import ASGITransport
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration

from tvbf.main import app
from tvbf.routers.debug_admin import TelemetryTestError


@pytest.fixture
async def admin_client(session, monkeypatch):
    """Depends on `session` so the conftest truncate teardown runs after each test."""
    monkeypatch.setenv("ADMIN_TOKEN", "shh")
    from tvbf.config import get_settings as _get_settings

    _get_settings.cache_clear()

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


@pytest.fixture
def sentry_events():
    """A live Sentry client whose `before_send` keeps every event and drops it,
    so nothing leaves the process. Closed afterwards so no later test runs with
    an initialised SDK."""
    events: list[dict] = []

    def keep(event, hint):
        events.append(event)
        return None

    sentry_sdk.init(
        dsn="https://public@bugsink.invalid/1",
        integrations=[StarletteIntegration(), FastApiIntegration()],
        default_integrations=False,
        before_send=keep,
        release="test-sha",
        environment="test",
    )
    try:
        yield events
    finally:
        sentry_sdk.get_client().close()
        sentry_sdk.init()


async def test_rejects_unauth(admin_client):
    r = await admin_client.post("/admin/debug/raise-test-error")
    assert r.status_code == 401


async def test_raises_its_own_exception_type(admin_client):
    with pytest.raises(TelemetryTestError, match="tvbf.debug.raise-test-error"):
        await admin_client.post(
            "/admin/debug/raise-test-error", headers={"Authorization": "Bearer shh"}
        )


async def test_is_reported_to_sentry_with_release_and_environment(admin_client, sentry_events):
    with pytest.raises(TelemetryTestError):
        await admin_client.post(
            "/admin/debug/raise-test-error", headers={"Authorization": "Bearer shh"}
        )
    [event] = sentry_events
    [exc] = event["exception"]["values"]
    assert exc["type"] == "TelemetryTestError"
    assert event["release"] == "test-sha"
    assert event["environment"] == "test"
