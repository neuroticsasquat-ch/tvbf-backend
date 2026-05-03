import asyncio
import time
from collections import deque

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


class TVMazeClient:
    def __init__(
        self,
        base_url: str,
        rate_calls: int,
        rate_window: float,
        retry_max_attempts: int = 5,
        retry_base_delay: float = 0.5,
        timeout: float = 30.0,
    ):
        self._base_url = base_url.rstrip("/")
        self._limiter = RateLimiter(rate_calls, rate_window)
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

    async def get_show(self, show_id: int) -> dict:
        url = f"{self._base_url}/shows/{show_id}"
        resp = await self._request(
            "GET", url, params=[("embed[]", "episodes"), ("embed[]", "seasons")]
        )
        return resp.json()

    async def get_show_updates(self) -> dict[int, int]:
        url = f"{self._base_url}/updates/shows"
        resp = await self._request("GET", url)
        return {int(k): int(v) for k, v in resp.json().items()}

    async def get_akas(self, show_id: int) -> list[dict]:
        url = f"{self._base_url}/shows/{show_id}/akas"
        resp = await self._request("GET", url)
        return resp.json()
