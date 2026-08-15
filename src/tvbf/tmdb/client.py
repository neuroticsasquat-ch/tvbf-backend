"""TMDB HTTP client: bearer auth, the shared TMDB budget, retry, `append_to_response`.

Same shape as the TV Maze client it replaced (retired in NEU-1050), different
economics. Three things differ and each one is load-bearing:

**Auth is a header.** TMDB accepts either the v3 `api_key` query parameter or
the account's API Read Access Token as `Authorization: Bearer`. Only the second
is used here. A credential in a query string is copied into access logs, proxy
logs, browser referrers and any error report that echoes a URL, and none of
those are places a secret can be revoked from. TMDB's docs call the bearer
style "v4 auth", which is a naming quirk rather than an endpoint version — it
authenticates the v3 endpoints this module calls.

**The rate is 11× TV Maze**, which is what makes leasing matter here and not
there. See `_budget` below.

**One request carries the whole show.** `append_to_response` rides
`external_ids`, `alternative_titles`, `aggregate_credits` and individual
seasons on the series request, so a field costs a column rather than a
multi-hour pass. The cap on that is the one number in this module worth
measuring rather than assuming — see `APPEND_TO_RESPONSE_LIMIT`.
"""

import asyncio
import logging
from collections.abc import Iterable, Sequence
from datetime import date

import httpx

from tvbf.rate_budget import Budget, Limiter, get_rate_limiter

log = logging.getLogger(__name__)

# The source name this client's budget is registered under in
# `tvbf.rate_budget.BUCKETS`.
SOURCE = "tmdb"


def _budget(rate_calls: int, rate_window: float) -> Budget:
    """This client's slice of the shared TMDB budget, leased a window at a time.

    TV Maze leases 1 — at 1.8 req/s a locked round trip per request is free. At
    20 req/s it is 20 serialised transactions per second through a single row,
    roughly 20× the pressure ADR-0006 was validated at, on the component whose
    failure mode is lock timeouts surfacing as job failures (NEU-1027).

    One window's worth is the largest lease that can ever be granted whole: the
    bucket's capacity *is* `calls`, so `_take` short-grants anything above it,
    and a bigger round number would just be `calls` wearing a misleading one.
    It buys at most one locked transaction per window — ~1/s at the default 20
    req/s — and preserves the cross-process guarantee exactly, because the
    block is deducted before any of it is spent.

    Sized from the configured rate rather than pinned, so lowering
    `TMDB_RATE_LIMIT_REQUESTS` lowers the lease with it instead of silently
    short-granting on every call.
    """
    return Budget(rate_calls, rate_window, lease=max(1, rate_calls))


# Ceiling on a backoff this client computes for itself. An upstream-supplied
# `Retry-After` is honoured in full — TMDB knows how long it wants to be left
# alone better than an exponent does.
_MAX_THROTTLE_WAIT = 60.0

# TMDB documents 20 connections per IP. Two TMDB processes will exist —
# scheduled jobs and in-app admin passes, the NEU-1008 shape — so a pool sized
# to the full ceiling breaches it whenever both run, which is the same rake the
# request budget already moved into Postgres to avoid. Half each.
_MAX_CONNECTIONS = 10

# How many entries `append_to_response` honours on one request.
#
# **Measured, not assumed** — the project's ~3.2-hour full-pass estimate rests
# on this number, so `scripts/probe_tmdb_append_limit.py` checks it against the
# live API rather than trusting the docs. Re-run it if this is ever in doubt.
#
# Measured 2026-08-09 against series 456 (The Simpsons, 36 seasons):
#
#   - 20 entries come back **in full** — every namespace present, and every
#     appended `season/N` carrying its complete `episodes` list, not a stub.
#     That is what makes one request per show the unit of work.
#   - 21 is rejected outright: HTTP 400, *"Too many append to response objects:
#     The maximum number of remote calls is 20."* TMDB fails loudly rather than
#     truncating, so an oversized request cannot ingest a show with seasons
#     quietly missing.
#   - Namespaces and `season/N` entries draw on the same 20. Eight namespaces
#     leaves twelve seasons.
#
# The documented figure and the real one agree, so milestone 3's estimate stands.
APPEND_TO_RESPONSE_LIMIT = 20

# The decided list (NEU-1031 §1). Eleven namespaces, leaving nine season slots
# out of the 20 above.
#
# Going from the provisional three to eleven costs +12,618 requests — about 11
# minutes on a pass that runs an hour and a half either way — because 2.9% of
# shows have more than nine seasons and pay one extra `get_tv_season` each.
# Against that, every namespace left off is a field that becomes a multi-hour
# backfill later, which is exactly how the TV Maze mirror accumulated four of
# them. Four namespaces are omitted and none of them for cost: `credits` is
# strictly weaker than `aggregate_credits`, `recommendations` and `similar` are
# TMDB-computed and volatile rather than catalog facts, and `reviews` is another
# product's user-generated content.
DEFAULT_APPEND: tuple[str, ...] = (
    "aggregate_credits",
    "alternative_titles",
    "content_ratings",
    "episode_groups",
    "external_ids",
    "images",
    "keywords",
    "screened_theatrically",
    "translations",
    "videos",
    "watch/providers",
)


# The widest date range `/tv/changes` accepts on one request, and the number the
# delta's window walking exists to respect (NEU-1035). TMDB rejects a wider span
# outright rather than clamping it, so a container that was down for three weeks
# cannot be caught up with a single oversized request.
CHANGES_MAX_WINDOW_DAYS = 14

# TMDB stops paging at 500 and answers a higher `page` with a 422. Reaching it
# would need 50,000 changed series in one window, which is not a day TMDB has —
# so this is a runaway guard rather than a paging strategy.
CHANGES_MAX_PAGE = 500


def is_gone_upstream(exc: BaseException) -> bool:
    """True when TMDB says this series no longer exists.

    404 only, and deliberately narrow — the same rule the retired TV Maze
    client applied, restated rather than imported because it is one predicate
    over an `httpx` exception with no TV Maze content in it, and because
    `tvmaze/client.py` was deleted outright by NEU-1050. Copying a line beats
    an import that retires; copying a module would not.

    `_request` already retries timeouts, network errors, 429s and 5xx to
    exhaustion before raising, so an `HTTPStatusError` reaching a run loop is
    never transient: a surfacing 5xx is a *persistent* upstream failure and must
    still count toward the consecutive-failure abort. A 404 is a permanent data
    condition — the daily export lists an id `/tv/{id}` no longer serves — and
    counting it says "upstream is broken" when upstream is fine (NEU-1006).

    Not widened to any 4xx: a 400 or 401 is a bug in our request or our config,
    and silently absorbing those would be worse than counting them.
    """
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 404


def season_key(season_number: int) -> str:
    """The `append_to_response` entry that rides one season's episode list."""
    return f"season/{season_number}"


def plan_append(
    season_numbers: Iterable[int],
    namespaces: Sequence[str] = DEFAULT_APPEND,
) -> tuple[list[str], list[int]]:
    """Split a show's seasons into what fits on the series request and what does not.

    Returns `(append, overflow)` — the `append_to_response` list for
    `get_tv_series`, and the season numbers that need their own
    `get_tv_season` call afterwards.

    The arithmetic is here rather than in the caller because it is the whole
    consequence of `APPEND_TO_RESPONSE_LIMIT`: namespaces and seasons draw on
    one budget, so adding a namespace costs a season. A caller slicing by hand
    would keep computing the old split after the cap moved, and TMDB answers a
    short list with a clean 200 — the arithmetic is the only place that error
    could be caught.
    """
    namespaces = list(namespaces)
    if len(namespaces) > APPEND_TO_RESPONSE_LIMIT:
        raise ValueError(
            f"{len(namespaces)} append_to_response namespaces exceeds the "
            f"{APPEND_TO_RESPONSE_LIMIT}-entry cap with no room for seasons"
        )
    seasons = list(season_numbers)
    room = APPEND_TO_RESPONSE_LIMIT - len(namespaces)
    return namespaces + [season_key(n) for n in seasons[:room]], seasons[room:]


class TMDBClient:
    def __init__(
        self,
        base_url: str,
        read_access_token: str | None,
        rate_calls: int,
        rate_window: float,
        retry_max_attempts: int = 5,
        retry_base_delay: float = 0.5,
        timeout: float = 30.0,
        limiter: Limiter | None = None,
    ):
        if not read_access_token:
            raise ValueError(
                "TMDB_READ_ACCESS_TOKEN is not set — TMDB requests cannot be authenticated"
            )
        self._base_url = base_url.rstrip("/")
        # Shared by default; pass `limiter` explicitly for an isolated budget.
        self._limiter = (
            limiter
            if limiter is not None
            else get_rate_limiter(SOURCE, _budget(rate_calls, rate_window))
        )
        self._retry_max = retry_max_attempts
        self._retry_base = retry_base_delay
        # On the client, not per request: a header set once cannot be forgotten
        # at a call site, and there is no code path that could put the token in
        # a URL because no code path builds one.
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {read_access_token}",
                "Accept": "application/json",
            },
            limits=httpx.Limits(max_connections=_MAX_CONNECTIONS),
        )

    async def __aenter__(self) -> "TMDBClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self._client.aclose()

    def _throttle_wait(self, retry_after: str | None, throttled: int) -> float:
        """How long to wait out a 429.

        `Retry-After` wins when it is usable. RFC 9110 also allows an HTTP-date
        form; TMDB sends delta-seconds, but an uncaught `ValueError` partway
        through a multi-hour pass is a poor way to discover otherwise, so an
        unparseable value falls back to backoff rather than raising.
        """
        if retry_after is not None:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                log.warning("unparseable Retry-After %r — backing off instead", retry_after)
        return min(self._retry_base * (2**throttled), _MAX_THROTTLE_WAIT)

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        attempt = 0
        # Counted separately from `attempt` on purpose — see the 429 branch.
        throttled = 0
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
                # A 429 says the shared budget is wrong about the ceiling, not
                # that this request failed, so it must not spend the retry
                # budget — exhausting retries on it would turn a paced client
                # into a failed run. It gets its own counter rather than none
                # at all, because a flat wait would hammer a throttling upstream
                # at a constant rate indefinitely, and on a free API that is how
                # access gets revoked.
                #
                # Deliberately unbounded: waiting a throttle out is the right
                # answer for a multi-hour pass, where failing loses the whole
                # run. Every wait logs, so a stuck job is visible in `task logs`
                # rather than silent.
                wait = self._throttle_wait(resp.headers.get("Retry-After"), throttled)
                throttled += 1
                log.warning("TMDB 429 on %s (#%d) — waiting %.2fs", url, throttled, wait)
                await asyncio.sleep(wait)
                continue

            if 500 <= resp.status_code < 600:
                if attempt + 1 >= self._retry_max:
                    resp.raise_for_status()
                await asyncio.sleep(self._retry_base * (2**attempt))
                attempt += 1
                continue

            resp.raise_for_status()
            return resp

    async def get_configuration(self) -> dict:
        """TMDB's image/base configuration.

        The cheapest authenticated call TMDB offers, which makes it the natural
        credential check: a bad token 401s here without touching the catalog.
        """
        resp = await self._request("GET", f"{self._base_url}/configuration")
        return resp.json()

    async def get_tv_series(
        self,
        series_id: int,
        *,
        append: Sequence[str] = DEFAULT_APPEND,
    ) -> dict:
        """One series, with `append` ridden along on the same request.

        `append` takes both namespaces (`external_ids`, `aggregate_credits`, …)
        and season entries (`season/1`) — they share one budget. Build it with
        `plan_append` rather than by hand.

        An oversized list is caught here rather than upstream. TMDB would reject
        it with a 400 anyway, so this changes nothing about correctness — it
        spends no token from a paced budget on a request that cannot succeed,
        and names the way out.
        """
        append = list(append)
        if len(append) > APPEND_TO_RESPONSE_LIMIT:
            raise ValueError(
                f"append_to_response takes at most {APPEND_TO_RESPONSE_LIMIT} entries, "
                f"got {len(append)} (TMDB answers 400). "
                f"Use plan_append() and fetch the overflow with get_tv_season()."
            )
        params = {"append_to_response": ",".join(append)} if append else None
        resp = await self._request("GET", f"{self._base_url}/tv/{series_id}", params=params)
        return resp.json()

    async def find_by_external_id(self, external_id: str, external_source: str) -> dict:
        """Look a series up by somebody else's id — `tvdb_id`, `imdb_id`, …

        Tiers 1 and 2 of the migration's mapping, and the only exact way in:
        TMDB offers no reverse lookup other than `/find`. The response is
        partitioned by media type (`tv_results`, `movie_results`, …) and each
        entry is a trimmed series object — notably **without** `status`, so a
        caller wanting the full row still fetches `get_tv_series` afterwards.

        `external_source` is TMDB's own enum and is passed through verbatim; an
        unsupported value answers 422 rather than an empty result, which is the
        loud failure worth keeping.
        """
        resp = await self._request(
            "GET",
            f"{self._base_url}/find/{external_id}",
            params={"external_source": external_source},
        )
        return resp.json()

    async def search_tv(self, query: str) -> dict:
        """Free-text series search — tier 3 of the migration's mapping.

        Deliberately **only** `query`. TMDB also takes `first_air_date_year`,
        and passing it would be the wrong kind of help: it filters upstream to
        an exact year, where the mapping rule allows ±1, and it would quietly
        change what "exactly one result" counts — a title with four candidates
        would come back as one and be accepted as unambiguous. The year check
        belongs on our side of the wire, against the unfiltered result set.

        Paging is not followed either. The caller wants `total_results == 1`, so
        a second page is by construction a result it would reject.
        """
        resp = await self._request("GET", f"{self._base_url}/search/tv", params={"query": query})
        return resp.json()

    async def get_tv_changes(self, *, start: date, end: date, page: int = 1) -> dict:
        """One page of `/tv/changes` — the series ids that changed in a date range.

        The delta's equivalent of TV Maze's `/updates/shows`, and shaped nothing
        like it: a **date range** rather than a per-show epoch, paged 100 at a
        time, and carrying only `id` and `adult`. A changed id therefore says
        nothing about *what* changed, so every hit costs a full re-fetch.

        Both bounds are inclusive upstream. The span guard is here for the same
        reason `get_tv_series` guards an oversized append: TMDB answers a wider
        range with an error either way, so this changes nothing about
        correctness — it spends no token from a paced budget on a request that
        cannot succeed, and names the way out.
        """
        span = (end - start).days
        if span > CHANGES_MAX_WINDOW_DAYS:
            raise ValueError(
                f"/tv/changes takes at most {CHANGES_MAX_WINDOW_DAYS} days per request, "
                f"got {span} ({start}..{end}). Use plan_windows() to walk the gap."
            )
        resp = await self._request(
            "GET",
            f"{self._base_url}/tv/changes",
            params={
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "page": page,
            },
        )
        return resp.json()

    async def get_tv_season(self, series_id: int, season_number: int) -> dict:
        """One season with its full episode list.

        The follow-up for seasons `plan_append` could not fit. A show needs one
        of these per overflow season, which is why the cap governs the cost of a
        full pass.
        """
        url = f"{self._base_url}/tv/{series_id}/season/{season_number}"
        resp = await self._request("GET", url)
        return resp.json()
