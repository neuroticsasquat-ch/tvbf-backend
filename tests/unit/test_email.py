"""Unit tests for the tvbf.email module: provider selection from config, plus
fake-transport tests for each client."""

from __future__ import annotations

import smtplib
from email.message import EmailMessage
from typing import Any

import httpx
import pytest
import respx

from tvbf.config import Settings
from tvbf.email import EmailSendError
from tvbf.email.factory import build_email_client
from tvbf.email.resend import ResendEmailClient
from tvbf.email.smtp import SmtpEmailClient


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "DATABASE_URL": "postgresql+asyncpg://u:p@h/db",
        "ADMIN_TOKEN": "t",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# build_email_client (provider selection)
# ---------------------------------------------------------------------------


def test_factory_selects_smtp_by_default() -> None:
    client = build_email_client(_settings())
    assert isinstance(client, SmtpEmailClient)


def test_factory_selects_resend_when_configured() -> None:
    client = build_email_client(_settings(EMAIL_PROVIDER="resend", RESEND_API_KEY="re_test"))
    assert isinstance(client, ResendEmailClient)


def test_factory_resend_requires_api_key() -> None:
    with pytest.raises(ValueError, match="RESEND_API_KEY"):
        build_email_client(_settings(EMAIL_PROVIDER="resend"))


def test_factory_rejects_unknown_provider() -> None:
    with pytest.raises(ValueError, match="unknown EMAIL_PROVIDER"):
        build_email_client(_settings(EMAIL_PROVIDER="postcard"))


def test_factory_is_case_insensitive() -> None:
    client = build_email_client(_settings(EMAIL_PROVIDER="RESEND", RESEND_API_KEY="re_test"))
    assert isinstance(client, ResendEmailClient)


# ---------------------------------------------------------------------------
# ResendEmailClient (fake HTTP transport via respx)
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_resend_send_posts_expected_payload() -> None:
    route = respx.post("https://api.resend.com/emails").mock(
        return_value=httpx.Response(200, json={"id": "abc"})
    )
    client = ResendEmailClient(api_key="re_test", from_address="from@x")

    await client.send(to="user@x", subject="hi", html="<b>hi</b>", text="hi")

    assert route.called
    req = route.calls.last.request
    assert req.headers["authorization"] == "Bearer re_test"
    import json as _json

    body = _json.loads(req.content)
    assert body == {
        "from": "from@x",
        "to": ["user@x"],
        "subject": "hi",
        "html": "<b>hi</b>",
        "text": "hi",
    }


@respx.mock
@pytest.mark.asyncio
async def test_resend_send_raises_on_http_error() -> None:
    respx.post("https://api.resend.com/emails").mock(
        return_value=httpx.Response(422, text="bad address")
    )
    client = ResendEmailClient(api_key="re_test", from_address="from@x")
    with pytest.raises(EmailSendError, match="422"):
        await client.send(to="x", subject="s", html="h", text="t")


@respx.mock
@pytest.mark.asyncio
async def test_resend_send_raises_on_transport_error() -> None:
    respx.post("https://api.resend.com/emails").mock(side_effect=httpx.ConnectError("nope"))
    client = ResendEmailClient(api_key="re_test", from_address="from@x")
    with pytest.raises(EmailSendError, match="transport error"):
        await client.send(to="x", subject="s", html="h", text="t")


# ---------------------------------------------------------------------------
# SmtpEmailClient (stub blocking sender)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_smtp_send_invokes_blocking_with_built_message() -> None:
    client = SmtpEmailClient(host="mailpit", port=1025, from_address="from@x")

    captured: list[EmailMessage] = []

    def _capture(msg: EmailMessage) -> None:
        captured.append(msg)

    client._send_blocking = _capture  # type: ignore[method-assign]

    await client.send(to="user@x", subject="hi", html="<b>hi</b>", text="hi")

    assert len(captured) == 1
    msg = captured[0]
    assert msg["From"] == "from@x"
    assert msg["To"] == "user@x"
    assert msg["Subject"] == "hi"
    # multipart/alternative with text + html parts
    parts = list(msg.iter_parts())
    assert len(parts) == 2
    assert parts[0].get_content().strip() == "hi"
    assert parts[1].get_content().strip() == "<b>hi</b>"


@pytest.mark.asyncio
async def test_smtp_send_wraps_smtp_exception() -> None:
    client = SmtpEmailClient(host="mailpit", port=1025, from_address="from@x")

    def _boom(_msg: EmailMessage) -> None:
        raise smtplib.SMTPException("nope")

    client._send_blocking = _boom  # type: ignore[method-assign]

    with pytest.raises(EmailSendError, match="smtp send failed"):
        await client.send(to="x", subject="s", html="h", text="t")


@pytest.mark.asyncio
async def test_smtp_send_wraps_oserror() -> None:
    client = SmtpEmailClient(host="mailpit", port=1025, from_address="from@x")

    def _boom(_msg: EmailMessage) -> None:
        raise ConnectionRefusedError("nope")

    client._send_blocking = _boom  # type: ignore[method-assign]

    with pytest.raises(EmailSendError):
        await client.send(to="x", subject="s", html="h", text="t")
