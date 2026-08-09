import asyncio
import logging

import httpx

from tvbf.rate_budget import Budget, Limiter, get_rate_limiter

log = logging.getLogger(__name__)

# The source name this client's budget is registered under in
# `tvbf.rate_budget.BUCKETS`.
SOURCE = "tvmaze"


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
        # No lease: at 1.8 req/s a locked round trip per request is free, and
        # token-for-token is what keeps TV Maze's calibration where ADR-0006
        # left it.
        self._limiter = (
            limiter
            if limiter is not None
            else get_rate_limiter(SOURCE, Budget(rate_calls, rate_window))
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
