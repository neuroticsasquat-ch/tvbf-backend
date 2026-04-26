import time

import httpx
import pytest
import respx

from tvbf.tvmaze.client import RateLimiter, TVMazeClient


async def test_rate_limiter_enforces_rate():
    limiter = RateLimiter(calls=3, window_seconds=1)
    start = time.monotonic()
    for _ in range(6):
        await limiter.acquire()
    elapsed = time.monotonic() - start
    assert elapsed >= 1.0, f"6 calls at 3/s should take >= 1s, took {elapsed:.3f}s"


@respx.mock
async def test_client_fetches_show_with_embeds():
    respx.get("https://api.tvmaze.com/shows/1").mock(
        return_value=httpx.Response(
            200, json={"id": 1, "name": "Under the Dome", "updated": 1, "genres": []}
        )
    )
    async with TVMazeClient(base_url="https://api.tvmaze.com", rate_calls=20, rate_window=1) as c:
        payload = await c.get_show(1)
    assert payload["id"] == 1
    assert respx.calls.last.request.url.params.get_list("embed[]") == ["episodes", "seasons"]


@respx.mock
async def test_client_fetches_updates_shows():
    respx.get("https://api.tvmaze.com/updates/shows").mock(
        return_value=httpx.Response(200, json={"1": 100, "2": 200})
    )
    async with TVMazeClient(base_url="https://api.tvmaze.com", rate_calls=20, rate_window=1) as c:
        updates = await c.get_show_updates()
    assert updates == {1: 100, 2: 200}


@respx.mock
async def test_client_retries_on_5xx_then_succeeds():
    route = respx.get("https://api.tvmaze.com/shows/42").mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(503),
            httpx.Response(200, json={"id": 42, "name": "ok", "updated": 1, "genres": []}),
        ]
    )
    async with TVMazeClient(
        base_url="https://api.tvmaze.com",
        rate_calls=20,
        rate_window=1,
        retry_max_attempts=3,
        retry_base_delay=0.01,
    ) as c:
        payload = await c.get_show(42)
    assert payload["id"] == 42
    assert route.call_count == 3


@respx.mock
async def test_client_honors_retry_after_on_429():
    route = respx.get("https://api.tvmaze.com/shows/7").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(200, json={"id": 7, "name": "ok", "updated": 1, "genres": []}),
        ]
    )
    async with TVMazeClient(
        base_url="https://api.tvmaze.com",
        rate_calls=20,
        rate_window=1,
        retry_max_attempts=3,
        retry_base_delay=0.01,
    ) as c:
        payload = await c.get_show(7)
    assert payload["id"] == 7
    assert route.call_count == 2


@respx.mock
async def test_client_does_not_retry_on_404():
    respx.get("https://api.tvmaze.com/shows/9999").mock(return_value=httpx.Response(404))
    async with TVMazeClient(
        base_url="https://api.tvmaze.com",
        rate_calls=20,
        rate_window=1,
        retry_max_attempts=3,
        retry_base_delay=0.01,
    ) as c:
        with pytest.raises(httpx.HTTPStatusError):
            await c.get_show(9999)
