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
