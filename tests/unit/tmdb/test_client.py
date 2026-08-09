import httpx
import pytest
import respx

from tvbf.rate_budget import Budget, get_rate_limiter
from tvbf.tmdb.client import (
    APPEND_TO_RESPONSE_LIMIT,
    DEFAULT_APPEND,
    TMDBClient,
    _budget,
    plan_append,
    season_key,
)
from tvbf.tvmaze.client import TVMazeClient

BASE = "https://api.themoviedb.org/3"
TOKEN = "eyJ-not-a-real-token"


def _client(
    *,
    read_access_token: str | None = TOKEN,
    retry_max_attempts: int = 5,
    retry_base_delay: float = 0.5,
) -> TMDBClient:
    return TMDBClient(
        base_url=BASE,
        read_access_token=read_access_token,
        rate_calls=20,
        rate_window=1,
        retry_max_attempts=retry_max_attempts,
        retry_base_delay=retry_base_delay,
    )


# --- auth -------------------------------------------------------------------


@respx.mock
async def test_token_travels_in_the_authorization_header():
    respx.get(f"{BASE}/tv/1396").mock(return_value=httpx.Response(200, json={"id": 1396}))
    async with _client() as c:
        await c.get_tv_series(1396)
    assert respx.calls.last.request.headers["Authorization"] == f"Bearer {TOKEN}"


@respx.mock
async def test_token_never_appears_in_the_url():
    """The whole point of bearer auth here (NEU-1028).

    A credential in a query string is copied into access logs, proxy logs and
    any error report that echoes a URL — none of which can revoke it.
    """
    respx.get(f"{BASE}/tv/1396").mock(return_value=httpx.Response(200, json={"id": 1396}))
    async with _client() as c:
        await c.get_tv_series(1396, append=["external_ids"])
    url = str(respx.calls.last.request.url)
    assert TOKEN not in url
    assert "api_key" not in url


def test_a_missing_token_raises_at_construction():
    """Config leaves the token optional so this lands without breaking running
    deploys; the client is where that becomes a loud failure."""
    with pytest.raises(ValueError, match="TMDB_READ_ACCESS_TOKEN"):
        _client(read_access_token=None)
    with pytest.raises(ValueError, match="TMDB_READ_ACCESS_TOKEN"):
        _client(read_access_token="")


# --- the shared budget ------------------------------------------------------


def test_client_shares_the_tmdb_budget_and_leases_a_window_at_a_time():
    """One bucket for the whole app (ADR-0006), leased a window's worth at a time.

    The identity check *is* the assertion: `get_rate_limiter` is cached on
    (source, budget), so matching here proves the client asked for exactly this
    budget — 20/s, lease 20 — rather than a private limiter that would let two
    processes hit TMDB at twice the configured rate (NEU-955, NEU-1008,
    NEU-1027 — the same rake three times).
    """
    assert _client()._limiter is get_rate_limiter("tmdb", Budget(20, 1, lease=20))


def test_the_lease_never_exceeds_what_the_bucket_can_grant():
    """Capacity *is* `calls`, so a lease above it short-grants every time and the
    number would be decorative. Sized from the rate, so lowering the rate lowers
    it too."""
    assert _budget(20, 1.0).lease == 20
    assert _budget(5, 1.0).lease == 5
    assert _budget(5, 1.0).lease <= _budget(5, 1.0).capacity


def test_tmdb_and_tvmaze_do_not_share_a_budget():
    """Different ceilings, different buckets. TV Maze's calibration is untouched."""
    tmdb = _client()
    tvmaze = TVMazeClient(base_url="https://api.tvmaze.com", rate_calls=18, rate_window=10)
    assert tmdb._limiter is not tvmaze._limiter


# --- append_to_response -----------------------------------------------------


@respx.mock
async def test_series_request_appends_the_default_namespaces():
    respx.get(f"{BASE}/tv/1396").mock(return_value=httpx.Response(200, json={"id": 1396}))
    async with _client() as c:
        await c.get_tv_series(1396)
    appended = respx.calls.last.request.url.params["append_to_response"]
    assert appended == ",".join(DEFAULT_APPEND)


@respx.mock
async def test_series_request_sends_no_append_param_for_an_empty_list():
    respx.get(f"{BASE}/tv/1396").mock(return_value=httpx.Response(200, json={"id": 1396}))
    async with _client() as c:
        await c.get_tv_series(1396, append=[])
    assert "append_to_response" not in respx.calls.last.request.url.params


@respx.mock
async def test_series_request_rejects_more_entries_than_tmdb_honours():
    """Measured: TMDB answers 21 entries with a 400 ("The maximum number of
    remote calls is 20"). Catching it here spends no token from a paced budget
    on a request that cannot succeed, and names the way out."""
    async with _client() as c:
        with pytest.raises(ValueError, match="at most 20 entries"):
            await c.get_tv_series(1396, append=[season_key(n) for n in range(1, 30)])
    assert not respx.calls  # never reached the network


@respx.mock
async def test_series_request_accepts_exactly_the_cap():
    respx.get(f"{BASE}/tv/1396").mock(return_value=httpx.Response(200, json={"id": 1396}))
    append = [season_key(n) for n in range(1, APPEND_TO_RESPONSE_LIMIT + 1)]
    async with _client() as c:
        await c.get_tv_series(1396, append=append)
    sent = respx.calls.last.request.url.params["append_to_response"].split(",")
    assert len(sent) == APPEND_TO_RESPONSE_LIMIT


@respx.mock
async def test_season_request_fetches_one_season():
    respx.get(f"{BASE}/tv/1396/season/3").mock(
        return_value=httpx.Response(200, json={"season_number": 3, "episodes": []})
    )
    async with _client() as c:
        season = await c.get_tv_season(1396, 3)
    assert season["season_number"] == 3


# --- /find ------------------------------------------------------------------


@respx.mock
async def test_find_by_external_id_passes_the_source_through():
    route = respx.get(f"{BASE}/find/tt0903747").mock(
        return_value=httpx.Response(200, json={"tv_results": [{"id": 1396}]})
    )
    async with _client() as c:
        found = await c.find_by_external_id("tt0903747", "imdb_id")

    assert found["tv_results"] == [{"id": 1396}]
    assert route.calls.last.request.url.params["external_source"] == "imdb_id"


@respx.mock
async def test_find_by_external_id_returns_the_empty_partitions_as_they_come():
    """No result is an ordinary answer, not an error — the mapping tiers fall
    through to the next one on it."""
    respx.get(f"{BASE}/find/999").mock(
        return_value=httpx.Response(200, json={"tv_results": [], "movie_results": []})
    )
    async with _client() as c:
        found = await c.find_by_external_id("999", "tvdb_id")

    assert found["tv_results"] == []


# --- plan_append ------------------------------------------------------------


def test_plan_append_rides_every_season_when_they_fit():
    append, overflow = plan_append([1, 2, 3])
    assert append == [*DEFAULT_APPEND, "season/1", "season/2", "season/3"]
    assert overflow == []


def test_plan_append_splits_a_show_that_exceeds_the_cap():
    """The Simpsons' 36 seasons against the decided 11 namespaces: 9 ride along."""
    room = APPEND_TO_RESPONSE_LIMIT - len(DEFAULT_APPEND)
    append, overflow = plan_append(range(1, 37))
    assert len(append) == APPEND_TO_RESPONSE_LIMIT
    assert append[: len(DEFAULT_APPEND)] == list(DEFAULT_APPEND)
    assert append[len(DEFAULT_APPEND) :] == [season_key(n) for n in range(1, 1 + room)]
    assert overflow == list(range(1 + room, 37))


def test_the_decided_namespace_list_leaves_room_for_seasons():
    """Eleven namespaces against a cap of 20 (NEU-1031 §1). A twelfth is a
    decision about the season budget, not a free addition."""
    assert len(DEFAULT_APPEND) == 11
    assert APPEND_TO_RESPONSE_LIMIT - len(DEFAULT_APPEND) == 9


def test_namespaces_and_seasons_draw_on_one_budget():
    """Adding a namespace costs a season — the reason this arithmetic lives next
    to the constant rather than in each caller."""
    _, baseline = plan_append(range(1, 37))
    _, one_more = plan_append(range(1, 37), namespaces=[*DEFAULT_APPEND, "one_more"])
    assert len(one_more) == len(baseline) + 1


def test_plan_append_preserves_season_zero():
    """TMDB parks specials in season 0, and `range`-shaped assumptions lose it."""
    append, _ = plan_append([0, 1, 2])
    assert "season/0" in append


def test_plan_append_rejects_a_namespace_list_that_leaves_no_room():
    with pytest.raises(ValueError, match="no room for seasons"):
        plan_append([1], namespaces=[f"ns{i}" for i in range(APPEND_TO_RESPONSE_LIMIT + 1)])


# --- retry / backoff --------------------------------------------------------


@respx.mock
async def test_retries_on_5xx_then_succeeds():
    route = respx.get(f"{BASE}/tv/42").mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(503),
            httpx.Response(200, json={"id": 42}),
        ]
    )
    async with _client(retry_max_attempts=3, retry_base_delay=0.01) as c:
        payload = await c.get_tv_series(42)
    assert payload["id"] == 42
    assert route.call_count == 3


@respx.mock
async def test_persistent_5xx_raises_once_the_retry_budget_is_spent():
    route = respx.get(f"{BASE}/tv/42").mock(return_value=httpx.Response(502))
    async with _client(retry_max_attempts=3, retry_base_delay=0.01) as c:
        with pytest.raises(httpx.HTTPStatusError):
            await c.get_tv_series(42)
    assert route.call_count == 3


@respx.mock
async def test_429_honours_retry_after_and_does_not_spend_the_retry_budget():
    """A 429 is the budget being wrong about the ceiling, not a failing request.
    Counting it would turn a paced client into a failed run."""
    route = respx.get(f"{BASE}/tv/7").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(200, json={"id": 7}),
        ]
    )
    async with _client(retry_max_attempts=2, retry_base_delay=0.01) as c:
        payload = await c.get_tv_series(7)
    assert payload["id"] == 7
    assert route.call_count == 4


@respx.mock
async def test_429_without_retry_after_backs_off():
    route = respx.get(f"{BASE}/tv/7").mock(
        side_effect=[httpx.Response(429), httpx.Response(200, json={"id": 7})]
    )
    async with _client(retry_max_attempts=3, retry_base_delay=0.01) as c:
        await c.get_tv_series(7)
    assert route.call_count == 2


@respx.mock
async def test_consecutive_429s_escalate_the_wait(monkeypatch):
    """A flat wait would hammer a throttling upstream at a constant rate forever,
    which on a free API is how access gets revoked. The 429 counter is what makes
    the backoff actually back off despite not spending the retry budget."""
    waits: list[float] = []

    async def _record(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr("tvbf.tmdb.client.asyncio.sleep", _record)
    respx.get(f"{BASE}/tv/7").mock(
        side_effect=[
            httpx.Response(429),
            httpx.Response(429),
            httpx.Response(429),
            httpx.Response(200, json={"id": 7}),
        ]
    )
    async with _client(retry_max_attempts=2, retry_base_delay=1.0) as c:
        await c.get_tv_series(7)
    assert waits == [1.0, 2.0, 4.0]


@respx.mock
async def test_self_computed_backoff_is_capped(monkeypatch):
    waits: list[float] = []

    async def _record(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr("tvbf.tmdb.client.asyncio.sleep", _record)
    respx.get(f"{BASE}/tv/7").mock(
        side_effect=[*[httpx.Response(429)] * 12, httpx.Response(200, json={"id": 7})]
    )
    async with _client(retry_max_attempts=2, retry_base_delay=1.0) as c:
        await c.get_tv_series(7)
    assert max(waits) == 60.0


@respx.mock
async def test_an_unparseable_retry_after_backs_off_instead_of_raising():
    """RFC 9110 also allows an HTTP-date. TMDB sends delta-seconds, but an
    uncaught ValueError partway through a multi-hour pass is a poor way to find
    out otherwise."""
    route = respx.get(f"{BASE}/tv/7").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}),
            httpx.Response(200, json={"id": 7}),
        ]
    )
    async with _client(retry_max_attempts=3, retry_base_delay=0.01) as c:
        payload = await c.get_tv_series(7)
    assert payload["id"] == 7
    assert route.call_count == 2


@respx.mock
async def test_a_negative_retry_after_does_not_become_a_negative_sleep():
    route = respx.get(f"{BASE}/tv/7").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "-5"}),
            httpx.Response(200, json={"id": 7}),
        ]
    )
    async with _client(retry_max_attempts=3, retry_base_delay=0.01) as c:
        await c.get_tv_series(7)
    assert route.call_count == 2


@respx.mock
async def test_retries_on_network_error_then_succeeds():
    route = respx.get(f"{BASE}/tv/42").mock(
        side_effect=[httpx.ConnectError("refused"), httpx.Response(200, json={"id": 42})]
    )
    async with _client(retry_max_attempts=3, retry_base_delay=0.01) as c:
        await c.get_tv_series(42)
    assert route.call_count == 2


@respx.mock
async def test_does_not_retry_on_404():
    route = respx.get(f"{BASE}/tv/9999").mock(return_value=httpx.Response(404))
    async with _client(retry_max_attempts=3, retry_base_delay=0.01) as c:
        with pytest.raises(httpx.HTTPStatusError):
            await c.get_tv_series(9999)
    assert route.call_count == 1


@respx.mock
async def test_does_not_retry_on_401():
    """A bad token is a config bug. Retrying it burns the budget and still fails."""
    route = respx.get(f"{BASE}/configuration").mock(return_value=httpx.Response(401))
    async with _client(retry_max_attempts=3, retry_base_delay=0.01) as c:
        with pytest.raises(httpx.HTTPStatusError):
            await c.get_configuration()
    assert route.call_count == 1
