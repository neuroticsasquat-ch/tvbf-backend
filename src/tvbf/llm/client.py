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
from collections.abc import Mapping
from typing import Any

import httpx

from tvbf.llm.registry import base_url_for
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
from tvbf.rate_budget import Limiter

log = logging.getLogger(__name__)

# The source name this client's budget is registered under in
# `tvbf.rate_budget.BUCKETS`. The registration itself, and defaulting `limiter=`
# to the shared bucket, are NEU-1099 — until then a caller supplies one, which
# is why the argument is required rather than optional-and-unpaced.
SOURCE = "deepinfra"


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
    if "json" not in prompt.system.lower():
        raise UnsupportedPromptError(
            'the instruction must contain the literal word "json": this provider '
            "rejects a request with response_format set and no such word in the "
            "prompt, with a 400 that no retry can help"
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
    """Which failures are worth another attempt."""
    if isinstance(exc, httpx.HTTPStatusError):
        return retry_for_status(exc.response.status_code, exc.response.headers)
    if isinstance(exc, httpx.TransportError):
        # Connect errors, read errors and timeouts — the request never got an
        # answer, so nothing is known about whether it would fail again.
        return Retry()
    # Anything else came off a response that did arrive. Asking again buys the
    # same broken body.
    return None


def _message_content(payload: Mapping[str, Any]) -> str:
    """The assistant's message text.

    Raises rather than returning `""` — the opposite of upmovies, and for a
    concrete reason: there, four stages each had their own verdict on a useless
    response and unifying them was an explicit non-goal, so an empty string let
    each parser reach its own. Here there is one caller and one verdict, and a
    response carrying no content is one the job should retry once.
    """
    choices = payload.get("choices") or []
    if not choices:
        raise LLMResponseInvalid("the response carried no choices")
    content = (choices[0].get("message") or {}).get("content")
    if not content:
        # `finish_reason` is what makes this actionable: "length" means the reply
        # was cut off at `max_tokens` and the fix is a higher ceiling, while
        # anything else means the model produced nothing and the fix is a
        # different prompt or model. It matters more than it looks — DeepSeek is
        # a reasoning model, and reasoning tokens count against `max_tokens`
        # while being invisible in the text that comes back.
        raise LLMResponseInvalid(
            f"the response carried no content (finish_reason={choices[0].get('finish_reason')!r})"
        )
    return content


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
        limiter: Limiter,
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
        self._limiter = limiter
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

    async def _post(self, body: dict[str, Any]) -> Mapping[str, Any]:
        """One paced HTTP attempt. Retried by `call_with_retry`, which is why the
        limiter is acquired here and not one level up: a retry is a request
        upstream sees, so it is a request the budget has to see too."""
        await self._limiter.acquire()
        response = await self._client.post("/chat/completions", json=body)
        response.raise_for_status()
        return response.json()

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
            payload = await call_with_retry(
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
        except ValueError as exc:
            # A 200 whose body is not JSON at all. `response.json()` lives inside
            # the retried operation because a body that will not decode is still
            # a call that was made and paid for, and `_classify` refuses to retry
            # it — asking again buys the same broken body.
            raise LLMResponseInvalid(
                f"the provider's response envelope did not decode as JSON: {exc}"
            ) from exc

        text = _message_content(payload)
        usage = payload.get("usage") or {}
        return LLMResponse(
            parsed=_parse_object(text),
            text=text,
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
        )
