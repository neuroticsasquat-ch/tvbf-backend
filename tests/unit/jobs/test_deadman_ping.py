"""The Uptime Kuma push the scheduled jobs feed their deadman with (NEU-1581).

Kuma reads a push as `GET <url>?status=up|down&msg=…&ping=…`; there is no
start ping and no path suffix. What each job pings when is pinned in its own
CLI tests — this file is the wire shape alone.
"""

import httpx
import pytest
import respx

from tvbf.jobs.scheduled import MAX_PING_MESSAGE, failure_message, ping_down, ping_up

PUSH = "https://kuma.example.com/api/push/abc123"


def _params(route: respx.Route) -> dict[str, str]:
    return dict(route.calls.last.request.url.params)


@respx.mock
async def test_up_sends_status_up_with_the_duration():
    route = respx.get(PUSH).mock(return_value=httpx.Response(200))

    await ping_up(PUSH, duration_ms=1234)

    assert route.call_count == 1
    assert _params(route) == {"status": "up", "msg": "ok", "ping": "1234"}


@respx.mock
async def test_down_sends_status_down_with_the_message():
    route = respx.get(PUSH).mock(return_value=httpx.Response(200))

    await ping_down(PUSH, "catalog update run 42 ended failed")

    assert route.call_count == 1
    assert _params(route) == {"status": "down", "msg": "catalog update run 42 ended failed"}


@respx.mock
async def test_down_url_encodes_and_truncates_the_message():
    route = respx.get(PUSH).mock(return_value=httpx.Response(200))

    await ping_down(PUSH, "a&b=c " + "x" * 500)

    msg = _params(route)["msg"]
    assert len(msg) == MAX_PING_MESSAGE
    assert msg.startswith("a&b=c ")
    assert "a%26b%3Dc" in str(route.calls.last.request.url)


@respx.mock
async def test_the_query_merges_over_one_pasted_from_kuma():
    """Kuma's UI shows the push URL with `?status=up&msg=OK&ping=` on the end;
    pasting it whole into Coolify must not leave a stale `status=up` on a down."""
    route = respx.get(PUSH).mock(return_value=httpx.Response(200))

    await ping_down(f"{PUSH}?status=up&msg=OK&ping=", "boom")

    assert _params(route) == {"status": "down", "msg": "boom"}


@pytest.mark.parametrize("url", [None, ""])
@respx.mock
async def test_an_unset_url_sends_nothing(url):
    await ping_up(url, duration_ms=1)
    await ping_down(url, "boom")

    assert not respx.calls


@respx.mock
async def test_a_ping_that_cannot_get_out_is_swallowed(caplog):
    respx.get(PUSH).mock(side_effect=httpx.ConnectError("no route"))

    await ping_up(PUSH, duration_ms=1)
    await ping_down(PUSH, "boom")

    assert "deadman ping" in caplog.text


def test_failure_message_is_the_type_and_first_line():
    assert failure_message(RuntimeError("first\nsecond")) == "RuntimeError: first"


def test_failure_message_without_text_is_the_type_alone():
    assert failure_message(TimeoutError()) == "TimeoutError"
