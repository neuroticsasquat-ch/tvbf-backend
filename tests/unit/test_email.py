"""Unit tests for the tvbf.email module: provider selection from config, plus
fake-transport tests for each client."""

from __future__ import annotations

import json
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


def test_factory_threads_reply_to_into_resend_client() -> None:
    client = build_email_client(
        _settings(
            EMAIL_PROVIDER="resend",
            RESEND_API_KEY="re_test",
            EMAIL_REPLY_TO_ADDRESS="help@x",
        )
    )
    assert isinstance(client, ResendEmailClient)
    assert client._default_reply_to == "help@x"


def test_factory_threads_reply_to_into_smtp_client() -> None:
    client = build_email_client(_settings(EMAIL_REPLY_TO_ADDRESS="help@x"))
    assert isinstance(client, SmtpEmailClient)
    assert client._default_reply_to == "help@x"


def test_factory_reply_to_defaults_to_none() -> None:
    client = build_email_client(_settings())
    assert isinstance(client, SmtpEmailClient)
    assert client._default_reply_to is None


def test_factory_treats_blank_reply_to_as_unset() -> None:
    client = build_email_client(_settings(EMAIL_REPLY_TO_ADDRESS=""))
    assert isinstance(client, SmtpEmailClient)
    assert client._default_reply_to is None


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


async def _resend_payload(client: ResendEmailClient, **send_kwargs: Any) -> dict[str, Any]:
    route = respx.post("https://api.resend.com/emails").mock(
        return_value=httpx.Response(200, json={"id": "abc"})
    )
    await client.send(to="user@x", subject="hi", html="<b>hi</b>", text="hi", **send_kwargs)
    body: dict[str, Any] = json.loads(route.calls.last.request.content)
    return body


@respx.mock
@pytest.mark.asyncio
async def test_resend_send_applies_default_reply_to() -> None:
    client = ResendEmailClient(api_key="re_test", from_address="from@x", default_reply_to="help@x")
    body = await _resend_payload(client)
    assert body["reply_to"] == "help@x"


@respx.mock
@pytest.mark.asyncio
async def test_resend_send_caller_reply_to_wins_over_default() -> None:
    client = ResendEmailClient(api_key="re_test", from_address="from@x", default_reply_to="help@x")
    body = await _resend_payload(client, reply_to="submitter@x")
    assert body["reply_to"] == "submitter@x"


@respx.mock
@pytest.mark.asyncio
async def test_resend_send_caller_reply_to_without_default() -> None:
    client = ResendEmailClient(api_key="re_test", from_address="from@x")
    body = await _resend_payload(client, reply_to="submitter@x")
    assert body["reply_to"] == "submitter@x"


@respx.mock
@pytest.mark.asyncio
async def test_resend_send_omits_reply_to_without_default_or_caller_value() -> None:
    client = ResendEmailClient(api_key="re_test", from_address="from@x")
    body = await _resend_payload(client)
    assert "reply_to" not in body


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


async def _smtp_message(client: SmtpEmailClient, **send_kwargs: Any) -> EmailMessage:
    captured: list[EmailMessage] = []
    client._send_blocking = captured.append  # type: ignore[method-assign]
    await client.send(to="user@x", subject="hi", html="<b>hi</b>", text="hi", **send_kwargs)
    assert len(captured) == 1
    return captured[0]


@pytest.mark.asyncio
async def test_smtp_send_applies_default_reply_to() -> None:
    client = SmtpEmailClient(
        host="mailpit", port=1025, from_address="from@x", default_reply_to="help@x"
    )
    msg = await _smtp_message(client)
    assert msg["Reply-To"] == "help@x"


@pytest.mark.asyncio
async def test_smtp_send_caller_reply_to_wins_over_default() -> None:
    client = SmtpEmailClient(
        host="mailpit", port=1025, from_address="from@x", default_reply_to="help@x"
    )
    msg = await _smtp_message(client, reply_to="submitter@x")
    assert msg.get_all("Reply-To") == ["submitter@x"]


@pytest.mark.asyncio
async def test_smtp_send_caller_reply_to_without_default() -> None:
    client = SmtpEmailClient(host="mailpit", port=1025, from_address="from@x")
    msg = await _smtp_message(client, reply_to="submitter@x")
    assert msg["Reply-To"] == "submitter@x"


@pytest.mark.asyncio
async def test_smtp_send_omits_reply_to_without_default_or_caller_value() -> None:
    client = SmtpEmailClient(host="mailpit", port=1025, from_address="from@x")
    msg = await _smtp_message(client)
    assert "Reply-To" not in msg


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
