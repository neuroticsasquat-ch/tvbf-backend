"""Turnstile siteverify — the three outcomes (NEU-1160 §5)."""

import httpx
import pytest
import respx

from tvbf.integrations.turnstile import (
    TurnstileRejected,
    TurnstileUnavailable,
    verify,
)

_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


@respx.mock
@pytest.mark.asyncio
async def test_success_returns_and_sends_the_documented_form():
    route = respx.post(_URL).mock(return_value=httpx.Response(200, json={"success": True}))
    await verify(token="tok", secret="sek", remote_ip="9.9.9.9")
    assert route.called
    body = dict(httpx.QueryParams(route.calls[0].request.content.decode()))
    assert body == {"secret": "sek", "response": "tok", "remoteip": "9.9.9.9"}


@respx.mock
@pytest.mark.asyncio
async def test_remoteip_is_omitted_when_unknown():
    route = respx.post(_URL).mock(return_value=httpx.Response(200, json={"success": True}))
    await verify(token="tok", secret="sek", remote_ip=None)
    body = dict(httpx.QueryParams(route.calls[0].request.content.decode()))
    assert "remoteip" not in body


@respx.mock
@pytest.mark.asyncio
async def test_success_false_is_rejected():
    respx.post(_URL).mock(
        return_value=httpx.Response(
            200, json={"success": False, "error-codes": ["timeout-or-duplicate"]}
        )
    )
    with pytest.raises(TurnstileRejected):
        await verify(token="tok", secret="sek", remote_ip=None)


@respx.mock
@pytest.mark.asyncio
async def test_error_codes_are_logged_not_returned(caplog):
    respx.post(_URL).mock(
        return_value=httpx.Response(200, json={"success": False, "error-codes": ["invalid-secret"]})
    )
    with caplog.at_level("WARNING"):
        with pytest.raises(TurnstileRejected):
            await verify(token="tok", secret="sek", remote_ip=None)
    assert "invalid-secret" in caplog.text


@respx.mock
@pytest.mark.asyncio
async def test_timeout_is_unavailable():
    respx.post(_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(TurnstileUnavailable):
        await verify(token="tok", secret="sek", remote_ip=None)


@respx.mock
@pytest.mark.asyncio
async def test_transport_error_is_unavailable():
    respx.post(_URL).mock(side_effect=httpx.ConnectError("nope"))
    with pytest.raises(TurnstileUnavailable):
        await verify(token="tok", secret="sek", remote_ip=None)


@respx.mock
@pytest.mark.asyncio
async def test_non_2xx_is_unavailable():
    respx.post(_URL).mock(return_value=httpx.Response(502, text="bad gateway"))
    with pytest.raises(TurnstileUnavailable):
        await verify(token="tok", secret="sek", remote_ip=None)


@respx.mock
@pytest.mark.asyncio
async def test_a_body_that_decodes_to_a_list_is_unavailable_not_a_500():
    # An interception page or a misrouted response. `payload.get("success")`
    # would raise AttributeError and escape the handler as a 500.
    respx.post(_URL).mock(return_value=httpx.Response(200, json=[{"success": True}]))
    with pytest.raises(TurnstileUnavailable):
        await verify(token="tok", secret="sek", remote_ip=None)


@respx.mock
@pytest.mark.asyncio
async def test_non_json_body_is_unavailable():
    respx.post(_URL).mock(return_value=httpx.Response(200, text="<html>captive portal</html>"))
    with pytest.raises(TurnstileUnavailable):
        await verify(token="tok", secret="sek", remote_ip=None)


@respx.mock
@pytest.mark.asyncio
async def test_a_body_missing_success_is_unavailable():
    respx.post(_URL).mock(return_value=httpx.Response(200, json={"error-codes": []}))
    with pytest.raises(TurnstileUnavailable):
        await verify(token="tok", secret="sek", remote_ip=None)
