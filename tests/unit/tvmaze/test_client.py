import asyncio
import time

import httpx
import pytest
import respx

from tvbf.rate_budget import RateLimiter, get_rate_limiter
from tvbf.tvmaze.client import TVMazeClient


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
async def test_client_honours_an_explicit_embed_list():
    respx.get("https://api.tvmaze.com/shows/168").mock(
        return_value=httpx.Response(
            200, json={"id": 168, "name": "Chuck", "updated": 1, "genres": []}
        )
    )
    async with TVMazeClient(base_url="https://api.tvmaze.com", rate_calls=20, rate_window=1) as c:
        await c.get_show(168, embed=["episodes", "seasons", "cast", "crew"])
    # All four combine in one upstream request, in the order given.
    assert respx.calls.last.request.url.params.get_list("embed[]") == [
        "episodes",
        "seasons",
        "cast",
        "crew",
    ]


@respx.mock
async def test_client_embeds_nothing_for_an_empty_embed_list():
    respx.get("https://api.tvmaze.com/shows/168").mock(
        return_value=httpx.Response(
            200, json={"id": 168, "name": "Chuck", "updated": 1, "genres": []}
        )
    )
    async with TVMazeClient(base_url="https://api.tvmaze.com", rate_calls=20, rate_window=1) as c:
        await c.get_show(168, embed=[])
    assert respx.calls.last.request.url.params.get_list("embed[]") == []


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


@respx.mock
async def test_get_show_episodes_asks_for_specials():
    """The episodes endpoint is the only source of specials, and only with
    specials=1 — the embed form omits them and ignores the flag."""
    respx.get("https://api.tvmaze.com/shows/168/episodes").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": 12, "season": 4, "number": 1, "name": "Chuck Versus the Anniversary"},
                {"id": 153062, "season": 4, "number": None, "name": "Buy Hard"},
            ],
        )
    )
    async with TVMazeClient(base_url="https://api.tvmaze.com", rate_calls=20, rate_window=1) as c:
        episodes = await c.get_show_episodes(168)
    assert respx.calls.last.request.url.params.get_list("specials") == ["1"]
    assert [e["number"] for e in episodes] == [1, None]


@respx.mock
async def test_get_show_episodes_omits_the_specials_param_when_disabled():
    respx.get("https://api.tvmaze.com/shows/168/episodes").mock(
        return_value=httpx.Response(200, json=[])
    )
    async with TVMazeClient(base_url="https://api.tvmaze.com", rate_calls=20, rate_window=1) as c:
        await c.get_show_episodes(168, specials=False)
    assert respx.calls.last.request.url.params.get_list("specials") == []


@respx.mock
async def test_get_akas_returns_list_of_dicts():
    route = respx.get("https://api.tvmaze.com/shows/123/akas").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "name": "Tokyo Revengers",
                    "country": {"code": "US", "name": "United States"},
                    "language": "en",
                },
                {
                    "name": "東京リベンジャーズ",
                    "country": {"code": "JP", "name": "Japan"},
                    "language": "ja",
                },
            ],
        )
    )
    async with TVMazeClient(
        base_url="https://api.tvmaze.com", rate_calls=18, rate_window=10.0
    ) as c:
        akas = await c.get_akas(123)
    assert route.called
    assert len(akas) == 2
    assert akas[0]["name"] == "Tokyo Revengers"


@respx.mock
async def test_get_akas_empty_list_for_shows_with_no_akas():
    respx.get("https://api.tvmaze.com/shows/999/akas").mock(
        return_value=httpx.Response(200, json=[])
    )
    async with TVMazeClient(
        base_url="https://api.tvmaze.com", rate_calls=18, rate_window=10.0
    ) as c:
        akas = await c.get_akas(999)
    assert akas == []


@respx.mock
async def test_get_person_requests_no_credit_embeds():
    """The request-side half of the ownership cutover (ADR-0003). This used to
    embed `guestcastcredits`; every credit table is written by the show axis
    now, so the person fetch is a plain attribute refresh."""
    respx.get("https://api.tvmaze.com/people/30856").mock(
        return_value=httpx.Response(200, json={"id": 30856, "name": "Zachary Levi", "updated": 1})
    )
    async with TVMazeClient(base_url="https://api.tvmaze.com", rate_calls=20, rate_window=1) as c:
        payload = await c.get_person(30856)
    assert payload["id"] == 30856
    assert respx.calls.last.request.url.params.get_list("embed[]") == []


@respx.mock
async def test_get_season_episodes_embeds_both_credit_sets():
    """One request per season is the whole cost argument (ADR-0003) — and it
    only holds if both embeds ride along. Dropping `guestcrew` would silently
    yield episodes with no crew, which is indistinguishable from an episode
    that genuinely has none."""
    respx.get("https://api.tvmaze.com/seasons/1234/episodes").mock(
        return_value=httpx.Response(200, json=[{"id": 1, "season": 1, "number": 1}])
    )
    async with TVMazeClient(base_url="https://api.tvmaze.com", rate_calls=20, rate_window=1) as c:
        episodes = await c.get_season_episodes(1234)

    assert [e["id"] for e in episodes] == [1]
    assert respx.calls.last.request.url.params.get_list("embed[]") == ["guestcast", "guestcrew"]


@respx.mock
async def test_client_fetches_updates_people():
    respx.get("https://api.tvmaze.com/updates/people").mock(
        return_value=httpx.Response(200, json={"1": 100, "2": 200})
    )
    async with TVMazeClient(base_url="https://api.tvmaze.com", rate_calls=20, rate_window=1) as c:
        updates = await c.get_person_updates()
    assert updates == {1: 100, 2: 200}


def _show_payload(show_id: int) -> dict:
    return {"id": show_id, "name": f"Show {show_id}", "updated": 1, "genres": []}


class _CountingLimiter(RateLimiter):
    """A limiter that records how often it was asked for a slot."""

    def __init__(self, calls: int, window_seconds: float):
        super().__init__(calls, window_seconds)
        self.acquired = 0

    async def acquire(self, n: int = 1) -> None:
        self.acquired += n
        await super().acquire(n)


@respx.mock
async def test_clients_in_one_process_share_one_rate_budget():
    """NEU-955: the budget is process-wide, so it holds in aggregate.

    Two clients at 2 req/s used to pace independently and put all four requests
    upstream inside one window — double the configured rate, with neither
    throttling the other. Asserts the rate directly rather than by elapsed
    time: no window may contain more than `rate_calls` requests across both
    clients.
    """
    stamps: list[float] = []

    def _record(_request: httpx.Request) -> httpx.Response:
        stamps.append(time.monotonic())
        return httpx.Response(200, json=_show_payload(1))

    for show_id in (1, 2, 3, 4):
        respx.get(f"https://api.tvmaze.com/shows/{show_id}").mock(side_effect=_record)

    async with (
        TVMazeClient(base_url="https://api.tvmaze.com", rate_calls=2, rate_window=1) as first,
        TVMazeClient(base_url="https://api.tvmaze.com", rate_calls=2, rate_window=1) as second,
    ):
        # Concurrently, so nothing is serialised by the await order.
        await asyncio.gather(
            first.get_show(1),
            first.get_show(2),
            second.get_show(3),
            second.get_show(4),
        )

    assert len(stamps) == 4
    for start in stamps:
        in_window = [s for s in stamps if start <= s < start + 1.0]
        assert len(in_window) <= 2, (
            f"{len(in_window)} requests inside one 1s window, budget is 2 — "
            "the clients are pacing on separate limiters"
        )


@respx.mock
async def test_an_injected_limiter_replaces_the_shared_one():
    """Explicit injection opts a caller out, so a test can still isolate itself.

    Asserted through the cache rather than the clock: if the shared limiter is
    never built, the clients cannot have been sharing one.
    """
    for show_id in (1, 2):
        respx.get(f"https://api.tvmaze.com/shows/{show_id}").mock(
            return_value=httpx.Response(200, json=_show_payload(show_id))
        )

    own = _CountingLimiter(calls=10, window_seconds=1)
    async with TVMazeClient(
        base_url="https://api.tvmaze.com", rate_calls=2, rate_window=1, limiter=own
    ) as client:
        await client.get_show(1)
        await client.get_show(2)

    assert own.acquired == 2
    assert get_rate_limiter.cache_info().currsize == 0, (
        "an injected limiter should not have built the process-wide one"
    )
