"""The TV Maze airdate oracle: keyless, free, and read for two endpoints (NEU-1145).

Not a mirror client. NEU-1050 retired the one this repo used to have and
NEU-1051 dropped the schema behind it, and none of that comes back: nothing here
writes a catalog row, and the only value that survives a call is **one integer
per `(show, season)`** — never TV Maze's dates, titles, numbering or anything
else. That minimisation is deliberate and it is what the licence position in
NEU-1145 §6 rests on, since TV Maze data is CC BY-SA 4.0 and the attribution
returns to the SPA footer alongside it.

Why this oracle and not another. Trakt's `first_aired` is a true UTC instant and
is not copyleft, which would have been the better shape — but it needs a
credential we do not have and an accuracy nobody here has measured, whereas
every accuracy claim in the spec is TV Maze-backed against 440 archived watch
rows. The iTunes Search API stamps its dates at midnight Pacific and would have
answered the question directly; it carries no Apple TV+ originals at all, which
is where most of the shift is.

**Two requests per show.** A `/lookup/shows` by external id, then one
`/shows/{id}/episodes` that carries the whole series. Against a work list of
~1,800 shows that is ~3,500 requests a night — about 2% of one IP's daily
allowance, which is the number that decided the work list's scope.
"""

import asyncio
import logging

import httpx

from tvbf.airdates.api_payloads import TVMazeEpisode, TVMazeShowRef
from tvbf.rate_budget import Budget, Limiter, get_rate_limiter

log = logging.getLogger(__name__)

# The source name this client's budget is registered under in
# `tvbf.rate_budget.BUCKETS`.
SOURCE = "tvmaze"


def _budget(rate_calls: int, rate_window: float) -> Budget:
    """This client's slice of the shared TV Maze budget.

    Built here rather than inline at the construction site for the reason
    `tmdb/client.py:_budget` is: `get_rate_limiter` is `@cache`d on the literal
    call, so a second caller writing the same numbers a different way would mint
    a second limiter and a second lease against one row. One function is what
    keeps every caller's key identical.

    No lease. At ~1.8 req/s a locked round trip per request is free, which is
    the calibration ADR-0006 was validated at and the reason `Budget.lease`
    defaults to 1 — do not "unify" it upward with TMDB's 25.
    """
    return Budget(rate_calls, rate_window)


class TVMazeOracleClient:
    """Read-only TV Maze access, paced by the shared `tvmaze` budget.

    Every response is parsed through `api_payloads` and nothing else escapes,
    which is what makes the minimised extraction NEU-1145 §6 rests on a property
    of this module rather than a promise made downstream.
    """

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
            limiter
            if limiter is not None
            else get_rate_limiter(SOURCE, _budget(rate_calls, rate_window))
        )
        self._retry_max = retry_max_attempts
        self._retry_base = retry_base_delay
        self._client = httpx.AsyncClient(timeout=timeout)

    async def __aenter__(self) -> "TVMazeOracleClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self._client.aclose()

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response | None:
        """Perform one request. `None` means upstream answered 404.

        A 404 is the ordinary answer to "does TV Maze know this show?", not a
        failure — most of the ~229k mirrored series are not in the work list at
        all, and the ones that are can still be absent upstream. Returning
        `None` keeps that out of the caller's exception handling, where it would
        be counted against the consecutive-failure abort and eventually stop a
        run that is working perfectly.

        Everything else follows the retired client's shape: timeouts and network
        errors retry, a 429 waits without spending the retry budget, a 5xx
        retries to exhaustion and then raises.
        """
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

            if resp.status_code == 404:
                return None

            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                wait = self._retry_base * (2**attempt)
                if retry_after is not None:
                    try:
                        wait = max(0.0, float(retry_after))
                    except ValueError:
                        log.warning("unparseable Retry-After %r — backing off instead", retry_after)
                log.warning("TV Maze 429 on %s — waiting %.2fs", url, wait)
                await asyncio.sleep(wait)
                continue  # 429 does not count against the retry budget

            if 500 <= resp.status_code < 600:
                if attempt + 1 >= self._retry_max:
                    resp.raise_for_status()
                await asyncio.sleep(self._retry_base * (2**attempt))
                attempt += 1
                continue

            resp.raise_for_status()
            return resp

    async def lookup_show(self, *, imdb_id: str | None, tvdb_id: int | None) -> int | None:
        """This show's TV Maze id, by external id. `None` when neither resolves.

        IMDb first, then TheTVDB, because that is the order of coverage: 557 of
        560 tracked shows carry at least one, and IMDb is the more widely
        populated of the pair. There is no title fallback and there will not be
        one — a title match here would silently pair a season's dates with the
        wrong series, which is the failure mode every other matching pass in
        this repo resolves to "no match" instead.
        """
        for param, value in (("imdb", imdb_id), ("thetvdb", tvdb_id)):
            if value is None:
                continue
            resp = await self._request(
                "GET", f"{self._base_url}/lookup/shows", params={param: value}
            )
            if resp is not None:
                return TVMazeShowRef.model_validate(resp.json()).tvmaze_id
        return None

    async def get_show_episodes(self, show_id: int) -> list[TVMazeEpisode]:
        """Every numbered episode of one show, in one request.

        **`specials=1` is deliberately not sent.** It would add the episodes TV
        Maze numbers `null`, and a null number cannot be paired with anything on
        our side, so not one of them could ever reach a verdict. Fetching rows
        guaranteed to be discarded would widen the extraction §6 calls
        deliberately minimised in exchange for nothing.
        """
        resp = await self._request("GET", f"{self._base_url}/shows/{show_id}/episodes")
        if resp is None:
            return []
        return [TVMazeEpisode.model_validate(entry) for entry in resp.json()]
