"""The one retry policy behind every LLM call.

**Ported from `upcoming-movies-backend/src/upmovies/llm/retry.py`** (NEU-1098),
trimmed: upmovies' `Attempts` counter went with the telemetry rows it existed to
stamp (`types.CallResult.attempts`), which this module does not keep.

Its own module rather than a loop inside `client.py`, unlike `tmdb/client.py` and
`airdates/client.py` which both hand-roll one. Two reasons. The judgement here is
subtler than theirs — a clamped `Retry-After`, jitter, and a status set that
distinguishes "ask again" from "this will fail identically four times" — and it
is separately testable without a wire. And under-retrying does not surface as an
error: the weekly pass isolates per-user failures, so a provider given up on one
attempt early reads as a user who simply got no recommendations.

Note that `httpx.AsyncHTTPTransport(retries=N)` does **not** do this job — it
retries connection *establishment* only, and 429/5xx is most of what needs
retrying here.

Deliberately free of `httpx` imports: how a status code and headers come off a
failure is the client's business, which is why `call_with_retry` takes a
`classify` callable rather than knowing about responses.
"""

import asyncio
import random
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

# Transient by nature: the same request stands a real chance of succeeding
# shortly. Anything else — a malformed request, a bad credential, a model id
# that does not exist — fails identically four times and is better raised at
# once. A probe under `scripts/` is what settles the model id, and a 404 on it
# should surface in seconds rather than after the whole backoff curve.
_RETRYABLE_STATUS = frozenset({408, 409, 429})


@dataclass(frozen=True)
class Retry:
    """A classifier's verdict that a failure is worth another attempt.

    An object rather than a bare `bool` so it can carry the provider's own
    `Retry-After` hint; `None` from a classifier means "do not retry".
    """

    retry_after: float | None = None


Classifier = Callable[[BaseException], Retry | None]


@dataclass(frozen=True)
class RetryPolicy:
    """How many attempts, how long each may take, and how long to wait between.

    `timeout` is generous because the workload is: one call carries a user's
    whole watch history and asks for 25 recommendations, and DeepSeek spends
    reasoning tokens before the first visible one.
    """

    max_retries: int = 3
    timeout: float = 60.0
    initial_backoff: float = 0.5
    max_backoff: float = 8.0
    jitter: float = 0.25
    max_retry_after: float = 60.0

    def delay_for(
        self,
        attempt: int,
        retry_after: float | None = None,
        *,
        rand: Callable[[], float] = random.random,
    ) -> float:
        """Seconds to wait after `attempt` (1-based) before trying again.

        A `Retry-After` wins over the curve — the provider knows its own
        rate-limit window better than an exponent does — but is **clamped** to
        `max_retry_after` rather than discarded when it exceeds it. Discarding it
        drops the wait back to the curve's first step, so a provider asking for
        two minutes would be retried within half a second and 429'd every time,
        spending the whole retry budget inside a window it had already said was
        closed. Waiting less than asked is the one option guaranteed to fail;
        capping is at worst a wasted attempt. A nonsensical value (zero or
        negative) carries no information and falls back to the curve.

        Otherwise: exponential, capped at `max_backoff`, then shortened by up to
        `jitter` of itself, so concurrent callers do not retry in lockstep.
        """
        if retry_after is not None and retry_after > 0:
            return min(retry_after, self.max_retry_after)
        backoff = min(self.max_backoff, self.initial_backoff * 2 ** (attempt - 1))
        return backoff * (1.0 - self.jitter * rand())


DEFAULT_RETRY_POLICY = RetryPolicy()


def retry_after_seconds(headers: Mapping[str, str] | None) -> float | None:
    """The `Retry-After` header in seconds, or None when absent or not expressed
    that way.

    The HTTP-date form is deliberately not parsed: it needs a clock and a
    timezone to mean anything, and falling back to the backoff curve is the safe
    failure — a slightly wrong wait, never a wrong number of attempts.
    """
    if not headers:
        return None
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def retry_for_status(status: int, headers: Mapping[str, str] | None) -> Retry | None:
    """The verdict on an HTTP status."""
    if status in _RETRYABLE_STATUS or status >= 500:
        return Retry(retry_after=retry_after_seconds(headers))
    return None


async def call_with_retry[T](
    operation: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy,
    classify: Classifier,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    rand: Callable[[], float] = random.random,
) -> T:
    """Run `operation` until it succeeds, a failure is judged not worth retrying,
    or the retries run out.

    The original exception propagates untouched — the client is what turns it
    into an `LLMError`, because that translation is where the taxonomy lives and
    a retry-flavoured wrapper here would have to be unwrapped there.
    """
    attempts = 0
    while True:
        attempts += 1
        try:
            return await operation()
        except Exception as exc:
            verdict = classify(exc)
            if verdict is None or attempts > policy.max_retries:
                raise
            await sleep(policy.delay_for(attempts, verdict.retry_after, rand=rand))
