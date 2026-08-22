"""Route tests for POST /contact (NEU-1164)."""

from __future__ import annotations

import httpx
import pytest
import respx
from httpx import ASGITransport, AsyncClient

from tvbf.config import get_settings
from tvbf.main import app

_SITEVERIFY = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


@pytest.fixture
async def client(session):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        yield c


@pytest.fixture
def notify_email_configured():
    settings = get_settings()
    prior = settings.feedback_notify_email
    settings.feedback_notify_email = "tom@example.com"
    try:
        yield
    finally:
        settings.feedback_notify_email = prior


# ---------------------------------------------------------------------------
# Unauthenticated access (AC 1 — any caller can POST)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_contact_unauthenticated(client):
    """Anybody can POST /contact without cookies or session."""
    r = await client.post(
        "/contact",
        json={"name": "Alice", "email": "a@b.com", "message": "Hi"},
        headers={"X-Forwarded-For": "9.9.9.1"},
    )
    assert r.status_code == 204, r.text


# ---------------------------------------------------------------------------
# Happy path (AC 1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_contact_happy_path(client, _stub_outbound_email, notify_email_configured):
    """With valid fields, Turnstile off (default), returns 204 and delivers
    an email with Reply-To set to the caller's email."""
    r = await client.post(
        "/contact",
        json={"name": "Alice", "email": "alice@example.com", "message": "Love the app!"},
        headers={"X-Forwarded-For": "9.9.9.2"},
    )
    assert r.status_code == 204, r.text
    assert len(_stub_outbound_email) == 1
    sent = _stub_outbound_email[0]
    assert sent["to"] == "tom@example.com"
    assert sent["subject"] == "[Contact] message from Alice"
    assert "Love the app!" in sent["text"]
    assert "Alice" in sent["text"]
    assert sent["reply_to"] == "alice@example.com"


# ---------------------------------------------------------------------------
# No notify email configured
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_contact_no_notify_email(client, _stub_outbound_email):
    """With no FEEDBACK_NOTIFY_EMAIL configured, returns 204 and sends nothing."""
    r = await client.post(
        "/contact",
        json={"name": "Bob", "email": "bob@example.com", "message": "Hello"},
        headers={"X-Forwarded-For": "9.9.9.3"},
    )
    assert r.status_code == 204, r.text
    assert _stub_outbound_email == []


# ---------------------------------------------------------------------------
# Turnstile (AC 2–4)
# ---------------------------------------------------------------------------


@pytest.fixture
def turnstile_on():
    settings = get_settings()
    prior_enabled = settings.turnstile_enabled
    prior_secret = settings.turnstile_secret_key
    settings.turnstile_enabled = True
    settings.turnstile_secret_key = "sek"
    try:
        yield
    finally:
        settings.turnstile_enabled = prior_enabled
        settings.turnstile_secret_key = prior_secret


@respx.mock
@pytest.mark.asyncio
async def test_contact_rejects_invalid_turnstile_token(client, turnstile_on):
    """With turnstile_enabled=true and a bad token, returns 400 captcha_invalid."""
    respx.post(_SITEVERIFY).mock(
        return_value=httpx.Response(200, json={"success": False, "error-codes": ["invalid-input"]})
    )
    r = await client.post(
        "/contact",
        json={"name": "Alice", "email": "a@b.com", "message": "Hi", "turnstile_token": "bad"},
        headers={"X-Forwarded-For": "9.9.9.4"},
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "captcha_invalid"


@respx.mock
@pytest.mark.asyncio
async def test_contact_no_token_while_enabled_is_400(client, turnstile_on):
    """With turnstile_enabled=true and no token, returns 400 captcha_required."""
    route = respx.post(_SITEVERIFY).mock(return_value=httpx.Response(200, json={"success": True}))
    r = await client.post(
        "/contact",
        json={"name": "Alice", "email": "a@b.com", "message": "Hi"},
        headers={"X-Forwarded-For": "9.9.9.5"},
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "captcha_required"
    assert not route.called


@respx.mock
@pytest.mark.asyncio
async def test_contact_turnstile_503(client, turnstile_on, caplog):
    """Siteverify timing out returns 503 captcha_unavailable."""
    respx.post(_SITEVERIFY).mock(side_effect=httpx.ConnectError("no route"))
    with caplog.at_level("WARNING", logger="tvbf.routers.contact"):
        r = await client.post(
            "/contact",
            json={"name": "Alice", "email": "a@b.com", "message": "Hi", "turnstile_token": "tok"},
            headers={"X-Forwarded-For": "9.9.9.6"},
        )
    assert r.status_code == 503
    assert r.json()["detail"] == "captcha_unavailable"
    record = next(rec for rec in caplog.records if rec.name == "tvbf.routers.contact")
    assert record.levelname == "WARNING"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_contact_validates_fields(client):
    """422 on missing/empty required fields."""
    r = await client.post("/contact", json={}, headers={"X-Forwarded-For": "9.9.9.7"})
    assert r.status_code == 422

    r = await client.post(
        "/contact",
        json={"name": "", "email": "a@b.com", "message": "x"},
        headers={"X-Forwarded-For": "9.9.9.8"},
    )
    assert r.status_code == 422

    r = await client.post(
        "/contact",
        json={"name": "X", "email": "not-an-email", "message": "x"},
        headers={"X-Forwarded-For": "9.9.9.9"},
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# IP throttle (AC 5–6)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_contact_ip_throttle(client, _stub_outbound_email, notify_email_configured):
    """The 6th attempt from one address returns 429; a different address succeeds."""
    body = {"name": "A", "email": "a@b.com", "message": "x"}
    for n in range(5):
        r = await client.post("/contact", json=body, headers={"X-Forwarded-For": "203.0.113.1"})
        assert r.status_code == 204, f"attempt {n} failed: {r.text}"

    r = await client.post("/contact", json=body, headers={"X-Forwarded-For": "203.0.113.1"})
    assert r.status_code == 429
    assert r.json()["detail"] == "rate_limited"
    assert r.headers["Retry-After"] == "3600"

    # A different address succeeds.
    r = await client.post(
        "/contact",
        json={"name": "B", "email": "b@c.com", "message": "y"},
        headers={"X-Forwarded-For": "203.0.113.2"},
    )
    assert r.status_code == 204, r.text


@respx.mock
@pytest.mark.asyncio
async def test_contact_turnstile_rejection_still_counts(
    client, turnstile_on, _stub_outbound_email, notify_email_configured
):
    """A Turnstile-rejected attempt still counts — the commit precedes verification."""
    # First 4 attempts succeed (mock a valid Turnstile response)
    route = respx.post(_SITEVERIFY).mock(return_value=httpx.Response(200, json={"success": True}))
    for n in range(4):
        r = await client.post(
            "/contact",
            json={"name": "A", "email": f"a{n}@b.com", "message": "x", "turnstile_token": "good"},
            headers={"X-Forwarded-For": "203.0.113.3"},
        )
        assert r.status_code == 204, f"attempt {n} failed: {r.text}"

    # 5th attempt: Turnstile rejects it (400), but the attempt was already committed
    route.mock(
        return_value=httpx.Response(200, json={"success": False, "error-codes": ["invalid-input"]})
    )
    r = await client.post(
        "/contact",
        json={"name": "A", "email": "a4@b.com", "message": "x", "turnstile_token": "bad"},
        headers={"X-Forwarded-For": "203.0.113.3"},
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "captcha_invalid"

    # 6th attempt should be throttled (the 5th was recorded even though it failed
    # Turnstile — the commit precedes verification)
    r = await client.post(
        "/contact",
        json={"name": "A", "email": "a5@b.com", "message": "x", "turnstile_token": "good"},
        headers={"X-Forwarded-For": "203.0.113.3"},
    )
    assert r.status_code == 429


# ---------------------------------------------------------------------------
# Email-send failure (AC 7)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_contact_email_failure_logged_and_returns_204(
    client, _stub_outbound_email, notify_email_configured, caplog
):
    """An EmailSendError is logged and the caller still sees 204."""
    from tvbf.email import EmailSendError
    from tvbf.routers import contact as contact_router

    original = contact_router.send_email

    async def _bomb(*, to, subject, html, text, reply_to=None):
        raise EmailSendError("boom")

    contact_router.send_email = _bomb  # type: ignore[assignment]
    try:
        with caplog.at_level("WARNING", logger="tvbf.routers.contact"):
            r = await client.post(
                "/contact",
                json={"name": "Alice", "email": "a@b.com", "message": "Hi"},
                headers={"X-Forwarded-For": "9.9.9.10"},
            )
        assert r.status_code == 204, r.text
        records = [rec for rec in caplog.records if rec.name == "tvbf.routers.contact"]
        assert any("contact notification send failed" in rec.message for rec in records)
    finally:
        contact_router.send_email = original  # type: ignore[assignment]
