import asyncio
import logging
import time
from collections import deque
from functools import cache
from typing import Protocol

import httpx

from tvbf.tvmaze.rate_budget import DatabaseRateLimiter

log = logging.getLogger(__name__)


class RateLimiter:
    """Sliding-window token bucket, per process. Allows `calls` per `window_seconds`.

    No longer the default — `get_rate_limiter` returns a `DatabaseRateLimiter`
    so the budget spans processes (ADR-0006). This survives as the isolated
    limiter tests pass via `limiter=`, which is what keeps the unit suite off
    the database.
    """

    def __init__(self, calls: int, window_seconds: float):
        self._calls = calls
        self._window = window_seconds
        self._timestamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            while self._timestamps and now - self._timestamps[0] >= self._window:
                self._timestamps.popleft()
            if len(self._timestamps) >= self._calls:
                wait = self._window - (now - self._timestamps[0])
                if wait > 0:
                    await asyncio.sleep(wait)
                    now = time.monotonic()
                    while self._timestamps and now - self._timestamps[0] >= self._window:
                        self._timestamps.popleft()
            self._timestamps.append(time.monotonic())


# Every budget `get_rate_limiter` has been asked for. Tracked separately from
# the cache because `cache_info()` reports a size, not the keys behind it.
_seen_budgets: set[tuple[int, float]] = set()


@cache
def get_rate_limiter(calls: int, window_seconds: float) -> DatabaseRateLimiter:
    """The limiter for one request budget, shared by every process.

    TV Maze's cap applies to us as a whole, not to each job. Every admin route
    builds its own `TVMazeClient`, so a per-instance limiter let two concurrent
    jobs each pace at the configured rate and hit upstream at twice it — over
    the cap, with neither throttling the other. Sharing one bucket means
    concurrent jobs split a single budget and simply run slower, which is the
    intended behaviour.

    The bucket lives in Postgres, so that holds across processes too — which it
    has to, now the daily update runs as its own process rather than as a task
    inside the app (ADR-0006). Caching stays worthwhile even so: the instance is
    cheap, but the cache is what makes a divergent budget detectable at all.

    Cached rather than built at import so the settings that size it are read
    when the first client is constructed. Tests reset it through
    `reset_rate_limiters()`, which `tests/conftest.py` calls between tests. Do
    not call `get_rate_limiter.cache_clear()` directly — it leaves
    `_seen_budgets` populated, so the cache and the seen set fall out of step.

    The cache is keyed by budget, so callers asking for *different* numbers get
    different buckets — which would reintroduce exactly the overshoot this
    exists to prevent. Every construction site reads the same `Settings`, so it
    cannot happen today; a second budget warns rather than failing silently,
    because the symptom otherwise is invisible (NEU-957). Size a new caller
    from settings too.

    The warning is per process, so it catches a divergence *within* one and not
    between two. Two processes reading different `TVMAZE_RATE_LIMIT_*` values
    would size the same shared bucket differently and neither would say so —
    a real limitation, and the reason both read the same env.
    """
    # The membership half only matters if someone bypassed `reset_rate_limiters`
    # and cleared the cache alone: the budget would then be a miss while still
    # in `_seen_budgets`, and warning about it would be noise.
    if _seen_budgets and (calls, window_seconds) not in _seen_budgets:
        log.warning(
            "additional TV Maze rate budget requested (%s per %ss; already have %s) — "
            "jobs on different budgets no longer share one limiter and will "
            "exceed the upstream cap together",
            calls,
            window_seconds,
            sorted(_seen_budgets),
        )
    _seen_budgets.add((calls, window_seconds))
    # Looked up as a module global on purpose: `tests/conftest.py` swaps this
    # name for the in-process `RateLimiter` so no unit test needs a database.
    return DatabaseRateLimiter(calls, window_seconds)


def reset_rate_limiters() -> None:
    """Drop the cached limiters and the budgets seen so far.

    For tests only — `tests/conftest.py` calls this between tests so the
    divergence warning does not leak into the next one.
    """
    get_rate_limiter.cache_clear()
    _seen_budgets.clear()


def is_gone_upstream(exc: BaseException) -> bool:
    """True when upstream says this entity no longer exists.

    404 only, and deliberately narrow.

    `_request` already retries timeouts, network errors, 429s and 5xx to
    exhaustion before raising, so an `HTTPStatusError` reaching a run loop is
    never transient: a 5xx that surfaces is a *persistent* upstream failure and
    must still count toward the consecutive-failure abort. A 404 is a permanent
    data condition — the entity is gone — and counting it says "upstream is
    broken" when upstream is fine (NEU-1006).

    Not widened to any 4xx: a 400 or 401 is a bug in our request or our config
    and must still abort. Silently absorbing those would be worse than counting
    them. 410 Gone is not included either — TV Maze does not send it, and an
    unexercised branch is speculative.
    """
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 404


class Limiter(Protocol):
    """Anything that can pace requests. `limiter=` accepts any of them."""

    async def acquire(self) -> None: ...


class TVMazeClient:
    def __init__(
        self,
        base_url: str,
        rate_calls: int,
        rate_window: float,
        retry_max_attempts: int = 5,
        retry_base_delay: float = 0.5,
        timeout: float = 30.0,
        limiter: Limiter | None = None,
    ):
        self._base_url = base_url.rstrip("/")
        # Shared by default; pass `limiter` explicitly for an isolated budget.
        self._limiter = (
            limiter if limiter is not None else get_rate_limiter(rate_calls, rate_window)
        )
        self._retry_max = retry_max_attempts
        self._retry_base = retry_base_delay
        self._client = httpx.AsyncClient(timeout=timeout)

    async def __aenter__(self) -> "TVMazeClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self._client.aclose()

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        attempt = 0
        while True:
            await self._limiter.acquire()
            try:
                resp = await self._client.request(method, url, **kwargs)
            except (httpx.TimeoutException, httpx.NetworkError):
                if attempt + 1 >= self._retry_max:
                    raise
                await asyncio.sleep(self._retry_base * (2**attempt))
                attempt += 1
                continue

            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                wait = (
                    float(retry_after)
                    if retry_after is not None
                    else self._retry_base * (2**attempt)
                )
                await asyncio.sleep(wait)
                continue  # 429 does not count against retry budget

            if 500 <= resp.status_code < 600:
                if attempt + 1 >= self._retry_max:
                    resp.raise_for_status()
                await asyncio.sleep(self._retry_base * (2**attempt))
                attempt += 1
                continue

            resp.raise_for_status()
            return resp

    _DEFAULT_EMBEDS = ("episodes", "seasons")

    async def get_show(self, show_id: int, *, embed: list[str] | None = None) -> dict:
        """Fetch a show, embedding `embed` (default: episodes + seasons).

        `episodes`, `seasons`, `cast` and `crew` all combine in one request.
        Passing an empty list embeds nothing.
        """
        embeds = tuple(embed) if embed is not None else self._DEFAULT_EMBEDS
        url = f"{self._base_url}/shows/{show_id}"
        resp = await self._request("GET", url, params=[("embed[]", e) for e in embeds])
        return resp.json()

    async def get_show_episodes(self, show_id: int, *, specials: bool = True) -> list[dict]:
        """Full episode list for a show, including specials by default.

        `embed[]=episodes` silently omits specials (episodes with a null
        `number`) and ignores a `specials=1` query param, so specials are only
        reachable here. This endpoint returns ALL episodes, so a caller using it
        does not also need `embed[]=episodes`.
        """
        url = f"{self._base_url}/shows/{show_id}/episodes"
        params = [("specials", "1")] if specials else []
        resp = await self._request("GET", url, params=params)
        return resp.json()

    async def get_season_episodes(self, season_id: int) -> list[dict]:
        """Every episode in a season, with its guest cast and episode crew.

        One request per season is what makes episode credits affordable at all:
        188,189 requests against 3.53M at episode grain (ADR-0003). Two things
        make this the right route rather than the show-level episode list:
        `/shows/{id}/episodes` honours neither embed, and this one includes
        specials without a `specials=1` param.
        """
        url = f"{self._base_url}/seasons/{season_id}/episodes"
        resp = await self._request(
            "GET", url, params=[("embed[]", "guestcast"), ("embed[]", "guestcrew")]
        )
        return resp.json()

    async def get_show_updates(self) -> dict[int, int]:
        url = f"{self._base_url}/updates/shows"
        resp = await self._request("GET", url)
        return {int(k): int(v) for k, v in resp.json().items()}

    async def get_person(self, person_id: int) -> dict:
        """A person's own attributes. No credit embeds — deliberately.

        Every credit table is written by the show axis now: show cast/crew from
        the show fetch, episode guest cast/crew from the season fetch
        (ADR-0003). `guestcastcredits` used to be embedded here, and dropping it
        is the request-side half of that cutover — the person axis survives only
        as an attribute refresh, because a rename or a new deathday marks no
        show updated and reaches us by no other route.
        """
        url = f"{self._base_url}/people/{person_id}"
        resp = await self._request("GET", url)
        return resp.json()

    async def get_person_updates(self) -> dict[int, int]:
        """Every person id upstream, mapped to its last-modified epoch."""
        url = f"{self._base_url}/updates/people"
        resp = await self._request("GET", url)
        return {int(k): int(v) for k, v in resp.json().items()}

    async def get_akas(self, show_id: int) -> list[dict]:
        url = f"{self._base_url}/shows/{show_id}/akas"
        resp = await self._request("GET", url)
        return resp.json()
