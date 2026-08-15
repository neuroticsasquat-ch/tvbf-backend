"""OpenAI-compatible chat-completions client: ask a hosted model for JSON.

**Ported from `upcoming-movies-backend/src/upmovies/llm/openai_compat.py`**
(NEU-1098). A thin first-party client over the `httpx` already in the dependency
list and already OTel-instrumented, rather than a routing library: the endpoint
surface actually used is one POST with four fields (upmovies ADR-0007).

Trimmed to the one thing the weekly pass needs — `complete_json` — and to the one
provider it uses. Gone with the four-stage gateway: `complete`/
`complete_with_usage`/`complete_call`, the `CallLog` telemetry, prefill support,
`Usage`'s cache-token disjointness (see `types.LLMResponse`), and offline and
pricing entirely.

**The two codebases will drift, and that is accepted in writing** (project spec
§6). A fix here will not reach upmovies and vice versa. A shared package is the
textbook answer and the wrong one for two personal repos with no shared
publishing setup: it would couple TVBF's release cadence to upmovies'. If a third
consumer appears, *that* is when extraction pays.
"""

import json
import logging
from typing import Any

import httpx
from pydantic import ValidationError

from tvbf.llm.api_payloads import ChatCompletion, ChatUsage
from tvbf.llm.registry import DEEPINFRA, base_url_for
from tvbf.llm.retry import (
    DEFAULT_RETRY_POLICY,
    Retry,
    RetryPolicy,
    call_with_retry,
    retry_for_status,
)
from tvbf.llm.types import (
    LLMRequestFailed,
    LLMResponse,
    LLMResponseInvalid,
    Prompt,
    UnsupportedPromptError,
)
from tvbf.rate_budget import Budget, Limiter, get_rate_limiter

log = logging.getLogger(__name__)

# The source name this client's budget is registered under in
# `tvbf.rate_budget.BUCKETS` (NEU-1099).
SOURCE = DEEPINFRA


def _budget(rate_calls: int, rate_window: float) -> Budget:
    """This client's slice of the shared DeepInfra budget.

    Built here rather than inline at the construction site for the reason
    `tmdb/client.py:_budget` and `airdates/client.py:_budget` are:
    `get_rate_limiter` is `@cache`d on the literal call, so a second caller
    writing the same numbers a different way would mint a second limiter and a
    second lease against one row. One function is what keeps every caller's key
    identical.

    No lease, which is `Budget`'s default and the pre-NEU-1027 behaviour token
    for token. Leasing exists to cut lock traffic on a source hit tens of times
    a second; this one is called once per changed user per week, so a locked
    round trip per request is free and a block held across a weekly gap is just
    tokens forfeited by a process that exits.
    """
    return Budget(rate_calls, rate_window)


def _to_wire(model: str, prompt: Prompt) -> dict[str, Any]:
    """Realize a `Prompt` in the OpenAI chat shape, JSON mode always on.

    `response_format` is unconditional because this client has exactly one call
    surface and it is `complete_json`. There is no `cache_control` to place: these
    providers cache automatically on the longest byte-identical request prefix,
    and nothing about this workload has a prefix worth caching (`types.Prompt`).
    """
    # **Measured, not assumed** (upmovies `llm/types.py`, live endpoint): DeepSeek
    # answers 400 when `response_format` is set and the word "json" appears
    # nowhere in the prompt. That is a non-retryable failure, so it would cost a
    # user their week's recommendations. Checked here rather than left as a
    # comment on the prompt builder so that "clean up the wording" cannot
    # silently break it — this is the site that sets `response_format`, so it is
    # the site that owes the constraint. NEU-1100 re-measures it live.
    #
    # Two deliberate strictnesses, both of them the safe direction of an unknown.
    # The match is **case-sensitive**, because "the provider lowercases before
    # looking" is not something anybody measured — a guard that accepts "JSON" on
    # that assumption would wave through the exact 400 it exists to stop. And only
    # `system` is inspected, which the milestone states as the rule and which is
    # stricter than the provider, whose own check reads the whole prompt: the word
    # belongs with the authored instruction, not incidentally inside a user
    # payload that varies per call. Both failures are loud, local and one word to
    # fix; the failure they replace is a lost week for a user.
    if "json" not in prompt.system:
        raise UnsupportedPromptError(
            'the instruction must contain the literal lower-case word "json": this '
            "provider rejects a request with response_format set and no such word "
            "in the prompt, with a 400 that no retry can help"
        )
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": prompt.system},
            {"role": "user", "content": prompt.user},
        ],
        "max_tokens": prompt.max_tokens,
        "response_format": {"type": "json_object"},
    }


def _classify(exc: BaseException) -> Retry | None:
    """Which failures are worth another attempt.

    Logs the ones it decides to retry. A retry curve nobody can see is how a
    provider that is half-failing reads as a provider that is fine: the weekly
    pass isolates per-user failures, so the visible symptom of a call that needed
    three attempts is nothing at all.
    """
    verdict: Retry | None = None
    if isinstance(exc, httpx.HTTPStatusError):
        verdict = retry_for_status(exc.response.status_code, exc.response.headers)
    elif isinstance(exc, httpx.TransportError):
        # Connect errors, read errors and timeouts — the request never got an
        # answer, so nothing is known about whether it would fail again.
        verdict = Retry()
    # Anything else came off a response that did arrive. Asking again buys the
    # same broken body, so `verdict` stays None.
    if verdict is not None:
        log.warning(
            "retrying the model provider after %s: %s (retry_after=%s)",
            type(exc).__name__,
            exc,
            verdict.retry_after,
        )
    return verdict


def _message_content(completion: ChatCompletion) -> str:
    """The assistant's message text.

    Raises rather than returning `""` — the opposite of upmovies, and for a
    concrete reason: there, four stages each had their own verdict on a useless
    response and unifying them was an explicit non-goal, so an empty string let
    each parser reach its own. Here there is one caller and one verdict, and a
    response carrying no content is one the job should retry once.
    """
    if not completion.choices:
        raise LLMResponseInvalid("the response carried no choices")
    choice = completion.choices[0]
    content = choice.message.content if choice.message is not None else None
    if not content:
        raise LLMResponseInvalid(
            f"the response carried no content (finish_reason={choice.finish_reason!r})"
        )
    return content


def _usage_or_zero(completion: ChatCompletion) -> ChatUsage:
    """The call's token counts, or zeroes with a warning.

    Zeroes rather than `None` because `LLMResponse` promises two ints that M4
    stores directly; optionality here would only move the question into a nullable
    column. The warning is what keeps a fabricated zero from passing silently for
    a measured one — no provider in scope omits the block, so if this ever fires
    the stored cost of that call is wrong and something upstream changed.
    """
    if completion.usage is None:
        log.warning("the model provider returned no usage block — token counts recorded as 0")
        return ChatUsage()
    return completion.usage


def _parse_object(text: str) -> dict[str, Any]:
    """The model's JSON object.

    A top-level object specifically: `response_format` makes the provider force
    one and the output contract is one (project spec §7), so an array or a scalar
    is a response that did not honour the request rather than a shape the caller
    should have to re-check.
    """
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        raise LLMResponseInvalid(f"the response did not decode as JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise LLMResponseInvalid(
            f"the response decoded as {type(parsed).__name__}, not a JSON object"
        )
    return parsed


class OpenAICompatClient:
    """Async context manager over one provider's chat-completions endpoint.

    The model is held here rather than passed per call: there is one model for
    one job, it comes from `RECOMMENDATION_MODEL`, and `complete_json(prompt)` is
    the contract M4 is written against. upmovies passes it per call because its
    gateway serves four stages that legitimately differ.
    """

    def __init__(
        self,
        *,
        provider: str,
        api_key: str | None,
        model: str | None,
        rate_calls: int,
        rate_window: float,
        limiter: Limiter | None = None,
        policy: RetryPolicy = DEFAULT_RETRY_POLICY,
    ):
        if not api_key:
            raise ValueError("DEEPINFRA_API_KEY is not set — LLM requests cannot be authenticated")
        if not model:
            raise ValueError(
                "RECOMMENDATION_MODEL is not set — there is no model to call. The exact "
                "DeepSeek model id available on DeepInfra is NEU-1100's to measure; it is "
                "deliberately not defaulted, because a wrong id asserted from memory is a "
                "non-retryable 404 in production."
            )
        self._model = model
        # Shared by default; pass `limiter` explicitly for an isolated budget.
        # The rate arguments stay required even for a caller that supplies one,
        # so a construction site always states the budget it means rather than
        # inheriting one from whichever default happened to be written here —
        # the same shape `TMDBClient` and `TVMazeOracleClient` take.
        self._limiter = (
            limiter
            if limiter is not None
            else get_rate_limiter(SOURCE, _budget(rate_calls, rate_window))
        )
        self._policy = policy
        self._client = httpx.AsyncClient(
            # Raises KeyError for a provider with no registry entry, at
            # construction rather than at the first call — a misconfigured job
            # should fail before it starts spending tokens.
            base_url=base_url_for(provider),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=policy.timeout,
        )

    async def __aenter__(self) -> "OpenAICompatClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _post(self, body: dict[str, Any]) -> httpx.Response:
        """One paced HTTP attempt. Retried by `call_with_retry`, which is why the
        limiter is acquired here and not one level up: a retry is a request
        upstream sees, so it is a request the budget has to see too.

        A budget that cannot be reached becomes `LLMRequestFailed` rather than
        escaping as whatever the limiter raised. It is `DatabaseRateLimiter`'s
        documented behaviour to **fail closed** — a bucket it cannot read raises
        rather than falling back to an in-process limiter — and since NEU-1099
        points this at the shared `deepinfra` row, a database blip surfaces here
        as a `SQLAlchemyError`. Left untranslated it is neither subclass, so a job
        dispatching on `LLMError` would miss it and lose its per-user isolation.
        `LLMRequestFailed` is the right half of the taxonomy: no request was made,
        and an unreachable budget will not heal inside a retry.
        """
        try:
            await self._limiter.acquire()
        except Exception as exc:
            raise LLMRequestFailed(f"the request budget could not be reached: {exc}") from exc
        response = await self._client.post("/chat/completions", json=body)
        response.raise_for_status()
        return response

    async def complete_json(self, prompt: Prompt) -> LLMResponse:
        """Ask the model for a JSON object.

        Raises `LLMRequestFailed` when no usable response arrived — the client has
        already retried whatever was worth retrying, so the caller must not — and
        `LLMResponseInvalid` when one arrived and could not be believed, which the
        caller retries exactly once (`types`).

        An `UnsupportedPromptError` from the prompt itself is neither: nothing was
        sent, so it is a programming error rather than a call outcome.
        """
        body = _to_wire(self._model, prompt)
        try:
            response = await call_with_retry(
                lambda: self._post(body), policy=self._policy, classify=_classify
            )
        except httpx.HTTPStatusError as exc:
            raise LLMRequestFailed(
                f"{exc.response.status_code} from the model provider: "
                # Bodies here are provider error envelopes, not model output, and
                # are the only place a wrong model id or a rejected
                # `response_format` says so. Truncated because nothing downstream
                # reads it and a long one buries the status in the log.
                f"{exc.response.text[:500]}"
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMRequestFailed(f"the request to the model provider failed: {exc}") from exc

        # Parsed outside the retry: a response that arrived and cannot be believed
        # is not worth asking for again — the same body comes back — which is why
        # `_classify` refuses to retry it and why this sits past the loop.
        try:
            completion = ChatCompletion.model_validate_json(response.content)
        except ValidationError as exc:
            raise LLMResponseInvalid(
                f"the provider's response envelope was not a chat completion: {exc}"
            ) from exc

        text = _message_content(completion)
        usage = _usage_or_zero(completion)
        return LLMResponse(
            parsed=_parse_object(text),
            text=text,
            input_tokens=usage.prompt_tokens,
            output_tokens=usage.completion_tokens,
        )
