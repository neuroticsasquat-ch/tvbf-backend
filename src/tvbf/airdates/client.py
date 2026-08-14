"""The TV Maze airdate oracle: keyless, free, and read for two endpoints (NEU-1145).

Not a mirror client. NEU-1050 retired the one this repo used to have and
NEU-1051 dropped the schema behind it, and none of that comes back: nothing here
writes a catalog row, and the only values that survive a call are **one integer
per `(show, season)`** and **one show id per show** — never TV Maze's dates,
titles, numbering or anything else. That minimisation is deliberate and it is
what the licence position in NEU-1145 §6, as amended by NEU-1148 §7, rests on:
TV Maze data is CC BY-SA 4.0, the attribution is in the SPA footer, and an
identifier is a bare fact about which record corresponds to which series rather
than any of the authored content the licence exists to govern.

Why this oracle and not another. Trakt's `first_aired` is a true UTC instant and
is not copyleft, which would have been the better shape — but it needs a
credential we do not have and an accuracy nobody here has measured, whereas
every accuracy claim in the spec is TV Maze-backed against 440 archived watch
rows. The iTunes Search API stamps its dates at midnight Pacific and would have
answered the question directly; it carries no Apple TV+ originals at all, which
is where most of the shift is.

**One request per show in steady state.** A `/shows/{id}/episodes` that carries
the whole series, and a `/lookup/shows` by external id only when
`airdates/show_state` has no usable id cached (NEU-1148) — which on a settled
work list is nearly never. It was two per show and ~3,500 requests a night
before the cache; either way it is a low single-digit percentage of one IP's
daily allowance, which is the number that decided the work list's scope.

**The lookup answers `301`, and the id is in the `Location` header.** Measured
against the live API on 2026-08-14: `/lookup/shows?imdb=tt2356777` returns
`301 Moved Permanently` to `https://api.tvmaze.com/shows/5`, and a miss returns
`404`. `follow_redirects` is deliberately left off — httpx would follow
internally, spending a second HTTP request that the limiter never sees, which
both breaks the two-requests-per-show budget above and understates our real
rate against an API that is free, keyless and unfunded. The redirect target is
the show object we do not need; the id we do need is already in the header.
"""

import asyncio
import logging
import re

import httpx

from tvbf.airdates.api_payloads import TVMazeEpisode, TVMazeShowRef
from tvbf.rate_budget import Budget, Limiter, get_rate_limiter

log = logging.getLogger(__name__)

# The source name this client's budget is registered under in
# `tvbf.rate_budget.BUCKETS`.
SOURCE = "tvmaze"

# What the lookup's `Location` header points at — `https://api.tvmaze.com/shows/5`.
_SHOW_PATH = re.compile(r"/shows/(\d+)")


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

    async def _request(
        self, method: str, url: str, *, redirect_ok: bool = False, **kwargs
    ) -> httpx.Response | None:
        """Perform one request. `None` means upstream answered 404.

        A 404 is the ordinary answer to "does TV Maze know this show?", not a
        failure — most of the ~229k mirrored series are not in the work list at
        all, and the ones that are can still be absent upstream. Returning
        `None` keeps that out of the caller's exception handling, where it would
        be counted against the consecutive-failure abort and eventually stop a
        run that is working perfectly.

        `redirect_ok` hands a 3xx back to the caller instead of raising, which
        only `lookup_show` wants: a redirect is that endpoint's *success*
        answer, and its `Location` carries the whole result. Everywhere else a
        redirect is unexpected and should still surface.

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

            if redirect_ok and resp.is_redirect:
                return resp

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

    @staticmethod
    def _show_id_from(resp: httpx.Response) -> int | None:
        """The TV Maze show id, from whichever shape the lookup answered in.

        Both are handled because the endpoint has already changed once: it is
        documented as returning the show object and currently answers `301` to
        `/shows/{id}` instead. Reading the id out of `Location` costs no extra
        request; falling back to the body means a revert upstream is not an
        outage here.
        """
        if resp.is_redirect:
            location = resp.headers.get("Location", "")
            match = _SHOW_PATH.search(location)
            if match is None:
                log.warning("TV Maze lookup redirected somewhere unexpected: %r", location)
                return None
            return int(match.group(1))
        return TVMazeShowRef.model_validate(resp.json()).tvmaze_id

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
                "GET",
                f"{self._base_url}/lookup/shows",
                params={param: value},
                redirect_ok=True,
            )
            if resp is None:
                continue
            show_id = self._show_id_from(resp)
            if show_id is not None:
                return show_id
        return None

    async def get_show_episodes(self, show_id: int) -> list[TVMazeEpisode] | None:
        """Every numbered episode of one show, in one request.

        **`None` means TV Maze does not carry this show; `[]` means it does and
        has no episodes to give.** The same distinction `tmdb/upsert.py` draws
        between "the caller did not append this namespace" and "upstream has
        none", and here it is load-bearing rather than tidy: since NEU-1148 the
        id we ask with can come from a cached row, so a 404 is the only signal
        that the cached link has gone stale. Collapsing the two would let such a
        show silently stop being reconciled forever, counted only among the
        seasons nothing could be compared for.

        **`specials=1` is deliberately not sent.** It would add the episodes TV
        Maze numbers `null`, and a null number cannot be paired with anything on
        our side, so not one of them could ever reach a verdict. Fetching rows
        guaranteed to be discarded would widen the extraction §6 calls
        deliberately minimised in exchange for nothing.
        """
        resp = await self._request("GET", f"{self._base_url}/shows/{show_id}/episodes")
        if resp is None:
            return None
        return [TVMazeEpisode.model_validate(entry) for entry in resp.json()]
