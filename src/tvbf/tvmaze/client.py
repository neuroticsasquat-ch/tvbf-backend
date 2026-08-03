import asyncio
import time
from collections import deque
from functools import cache

import httpx


class RateLimiter:
    """Sliding-window token bucket. Allows up to `calls` calls per `window_seconds`."""

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


@cache
def get_rate_limiter(calls: int, window_seconds: float) -> RateLimiter:
    """The process-wide limiter for one request budget.

    TV Maze's cap applies to us as a whole, not to each job. Every admin route
    builds its own `TVMazeClient`, so a per-instance limiter let two concurrent
    jobs each pace at the configured rate and hit upstream at twice it — over
    the cap, with neither throttling the other. Sharing one bucket means
    concurrent jobs split a single budget and simply run slower, which is the
    intended behaviour.

    Cached rather than built at import so the settings that size it are read
    when the first client is constructed. Tests reset it via `cache_clear()`
    (`functools.cache` exposes it the same as `lru_cache`); `tests/conftest.py`
    does that between tests so timestamps never leak.

    The cache is keyed by budget, so callers asking for *different* numbers get
    different buckets — which would reintroduce exactly the overshoot this
    exists to prevent. In practice it cannot happen: every construction site
    reads the same `Settings`. Size a new caller from settings too.
    """
    return RateLimiter(calls, window_seconds)


class TVMazeClient:
    def __init__(
        self,
        base_url: str,
        rate_calls: int,
        rate_window: float,
        retry_max_attempts: int = 5,
        retry_base_delay: float = 0.5,
        timeout: float = 30.0,
        limiter: RateLimiter | None = None,
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

    async def get_show_updates(self) -> dict[int, int]:
        url = f"{self._base_url}/updates/shows"
        resp = await self._request("GET", url)
        return {int(k): int(v) for k, v in resp.json().items()}

    async def get_person(self, person_id: int) -> dict:
        """A person plus their guest-cast credits, in one request.

        Only `guestcastcredits` is embedded. `castcredits` and `crewcredits`
        are free to request but are written by the show axis, and person-side
        credits carry no ordering — writing them would clobber billing order.
        """
        url = f"{self._base_url}/people/{person_id}"
        resp = await self._request("GET", url, params=[("embed[]", "guestcastcredits")])
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
