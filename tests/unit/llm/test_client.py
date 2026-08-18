"""The LLM client's wire behaviour (NEU-1098).

`respx` throughout — **no test ever calls DeepInfra** (project spec §12). The
three shapes the ticket names are the happy path, a malformed response and a
transport failure; the rest pin decisions that are silent when wrong, which is
the same reason `tests/unit/airdates/test_client.py` exists at all.
"""

import json

import httpx
import pytest
import respx

from tvbf.llm.client import OpenAICompatClient, _budget
from tvbf.llm.registry import DEEPINFRA
from tvbf.llm.retry import RetryPolicy
from tvbf.llm.types import (
    LLMRequestFailed,
    LLMResponseInvalid,
    Prompt,
    UnsupportedPromptError,
)
from tvbf.rate_budget import Budget, RateLimiter, get_rate_limiter

BASE = "https://api.deepinfra.com/v1/openai"
COMPLETIONS = f"{BASE}/chat/completions"

# The instruction has to carry the literal word "json" or the provider 400s.
PROMPT = Prompt(system="Answer as a json object.", user="what should I watch?")


def _client(**overrides) -> OpenAICompatClient:
    kwargs = {
        "provider": DEEPINFRA,
        "api_key": "test-key",
        "model": "deepseek-ai/Some-Model",
        "rate_calls": 5,
        "rate_window": 1,
        # An isolated in-process limiter, so no unit test needs a database —
        # which is also what keeps the shared `deepinfra` bucket (NEU-1099) out
        # of the unit suite. The test below is the one that asks for the default.
        "limiter": RateLimiter(20, 1),
        # No real sleeping: one attempt, so the backoff curve is never walked
        # except where a test asks for it.
        "policy": RetryPolicy(max_retries=0),
    }
    return OpenAICompatClient(**(kwargs | overrides))


def _completion(content: str, *, prompt_tokens: int = 11, completion_tokens: int = 7) -> dict:
    return {
        "choices": [
            {"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    }


class TestHappyPath:
    @respx.mock
    async def test_it_returns_the_parsed_object_the_raw_text_and_both_token_counts(self):
        route = respx.post(COMPLETIONS).mock(
            return_value=httpx.Response(
                200,
                json=_completion(
                    '{"recommendations": [{"title": "Slow Horses", "release_year": 2022}]}',
                    prompt_tokens=1234,
                    completion_tokens=567,
                ),
            )
        )

        async with _client() as client:
            response = await client.complete_json(PROMPT)

        assert response.parsed == {
            "recommendations": [{"title": "Slow Horses", "release_year": 2022}]
        }
        # The raw text is kept beside the parse because M4 stores it verbatim: an
        # unresolvable title is only diagnosable from what the model actually said.
        assert response.text.startswith('{"recommendations"')
        # Stored directly by M4 and never derived later, so they are asserted as
        # the provider's own numbers rather than as anything computed.
        assert (response.input_tokens, response.output_tokens) == (1234, 567)
        assert route.call_count == 1

    @respx.mock
    async def test_the_request_carries_json_mode_the_model_and_both_messages(self):
        route = respx.post(COMPLETIONS).mock(
            return_value=httpx.Response(200, json=_completion("{}"))
        )

        async with _client() as client:
            await client.complete_json(Prompt(system="json please", user="payload", max_tokens=99))

        sent = json.loads(respx.calls.last.request.content)
        assert sent["model"] == "deepseek-ai/Some-Model"
        assert sent["max_tokens"] == 99
        # Unconditional: this client has one call surface and it is complete_json.
        assert sent["response_format"] == {"type": "json_object"}
        assert sent["messages"] == [
            {"role": "system", "content": "json please"},
            {"role": "user", "content": "payload"},
        ]
        assert route.calls.last.request.headers["Authorization"] == "Bearer test-key"

    @respx.mock
    async def test_a_missing_usage_block_reads_as_zero_rather_than_failing(self):
        """A response with no `usage` is still a usable answer. Zero is honest —
        nothing else in the payload could say what it cost."""
        respx.post(COMPLETIONS).mock(
            return_value=httpx.Response(
                200, json={"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]}
            )
        )

        async with _client() as client:
            response = await client.complete_json(PROMPT)

        assert (response.input_tokens, response.output_tokens) == (0, 0)


class TestMalformedResponse:
    """Everything here is `LLMResponseInvalid`, which the weekly pass retries
    exactly once. Getting one of these classified as `LLMRequestFailed` would
    cost a user their week's recommendations for a one-off bad parse."""

    @respx.mock
    async def test_content_that_is_not_json_is_invalid_not_a_request_failure(self):
        respx.post(COMPLETIONS).mock(
            return_value=httpx.Response(200, json=_completion("Sure! Here are some shows:"))
        )

        async with _client() as client:
            with pytest.raises(LLMResponseInvalid, match="did not decode as JSON"):
                await client.complete_json(PROMPT)

    @respx.mock
    async def test_a_top_level_array_is_invalid(self):
        """`response_format` makes the provider force an object and the output
        contract is one, so an array did not honour the request — it is not a
        shape every caller has to re-check."""
        respx.post(COMPLETIONS).mock(
            return_value=httpx.Response(200, json=_completion('[{"title": "Slow Horses"}]'))
        )

        async with _client() as client:
            with pytest.raises(LLMResponseInvalid, match="not a JSON object"):
                await client.complete_json(PROMPT)

    @respx.mock
    async def test_an_empty_content_reports_the_finish_reason(self):
        """`length` means the reply was cut off at `max_tokens` and the fix is a
        higher ceiling; anything else means the model produced nothing. The two
        are indistinguishable from the text, and DeepSeek's reasoning tokens
        count against the ceiling while never appearing in it."""
        respx.post(COMPLETIONS).mock(
            return_value=httpx.Response(
                200, json={"choices": [{"message": {"content": ""}, "finish_reason": "length"}]}
            )
        )

        async with _client() as client:
            with pytest.raises(LLMResponseInvalid, match="finish_reason='length'"):
                await client.complete_json(PROMPT)

    @respx.mock
    async def test_a_response_with_no_choices_is_invalid(self):
        respx.post(COMPLETIONS).mock(return_value=httpx.Response(200, json={"choices": []}))

        async with _client() as client:
            with pytest.raises(LLMResponseInvalid, match="no choices"):
                await client.complete_json(PROMPT)

    @respx.mock
    async def test_an_envelope_that_is_not_json_at_all_is_invalid(self):
        """A 200 whose body is not even a provider envelope. Not retried by the
        client — asking again buys the same broken body — and not a transport
        failure either, because a response did arrive."""
        respx.post(COMPLETIONS).mock(return_value=httpx.Response(200, text="<html>502</html>"))

        async with _client() as client:
            with pytest.raises(LLMResponseInvalid, match="was not a chat completion"):
                await client.complete_json(PROMPT)

    @respx.mock
    async def test_an_envelope_that_decodes_to_a_list_is_invalid(self):
        """A 200 that decodes fine but is not a chat completion. Before the typed
        parser this walked a `.get()` chain into an `AttributeError`, which is
        neither half of the taxonomy and so escaped a job dispatching on
        `LLMError`."""
        respx.post(COMPLETIONS).mock(return_value=httpx.Response(200, json=[]))

        async with _client() as client:
            with pytest.raises(LLMResponseInvalid, match="was not a chat completion"):
                await client.complete_json(PROMPT)

    @respx.mock
    async def test_a_non_numeric_token_count_is_invalid_rather_than_a_crash(self):
        """Same hole one field along: `int("lots")` is a `ValueError` nobody
        catches. M4 stores these counts directly, so a count that is not a number
        is a response that cannot be believed."""
        respx.post(COMPLETIONS).mock(
            return_value=httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": "lots", "completion_tokens": 7},
                },
            )
        )

        async with _client() as client:
            with pytest.raises(LLMResponseInvalid, match="was not a chat completion"):
                await client.complete_json(PROMPT)

    @respx.mock
    async def test_unknown_provider_fields_are_ignored_not_refused(self):
        """A provider adding a key is not a reason to fail a call that otherwise
        came back fine — and both of these are real keys this module does not read
        (`prompt_tokens_details` is the cache split `pricing.py` would have needed)."""
        respx.post(COMPLETIONS).mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "chatcmpl-1",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "logprobs": None,
                            "message": {"role": "assistant", "content": '{"ok": true}'},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 3,
                        "completion_tokens": 4,
                        "total_tokens": 7,
                        "prompt_tokens_details": {"cached_tokens": 2},
                    },
                },
            )
        )

        async with _client() as client:
            response = await client.complete_json(PROMPT)

        assert response.parsed == {"ok": True}
        # The whole `prompt_tokens`, not the uncached remainder: upmovies subtracts
        # the cached count to keep four counts disjoint for `price()`, and
        # `pricing.py` is not ported.
        assert response.input_tokens == 3


class TestRequestFailure:
    """Everything here is `LLMRequestFailed`, which the weekly pass must **not**
    retry: the client has already retried whatever was worth retrying."""

    @respx.mock
    async def test_a_transport_failure_is_a_request_failure(self):
        respx.post(COMPLETIONS).mock(side_effect=httpx.ConnectError("connection refused"))

        async with _client() as client:
            with pytest.raises(LLMRequestFailed, match="request to the model provider failed"):
                await client.complete_json(PROMPT)

    @respx.mock
    async def test_a_timeout_is_a_request_failure(self):
        respx.post(COMPLETIONS).mock(side_effect=httpx.ReadTimeout("too slow"))

        async with _client() as client:
            with pytest.raises(LLMRequestFailed):
                await client.complete_json(PROMPT)

    @respx.mock
    async def test_an_unreachable_request_budget_is_a_request_failure(self):
        """`DatabaseRateLimiter` **fails closed** — a bucket it cannot read raises
        rather than falling back to an in-process limiter — so once NEU-1099 points
        this at the shared `deepinfra` row, a database blip arrives here. Untranslated
        it is neither subclass and a job dispatching on `LLMError` loses its per-user
        isolation."""
        route = respx.post(COMPLETIONS).mock(
            return_value=httpx.Response(200, json=_completion("{}"))
        )

        class _ClosedBudget:
            async def acquire(self, n: int = 1) -> None:
                raise RuntimeError("could not reach catalog.rate_budget")

        async with _client(limiter=_ClosedBudget()) as client:
            with pytest.raises(LLMRequestFailed, match="request budget could not be reached"):
                await client.complete_json(PROMPT)
        # And no request was made, which is why it is a request failure and not a
        # response one.
        assert route.call_count == 0

    @respx.mock
    async def test_a_400_carries_the_providers_own_error_body(self):
        """The body is the only place a rejected `response_format` or a wrong
        model id says so, and NEU-1100 is the ticket that reads it."""
        respx.post(COMPLETIONS).mock(
            return_value=httpx.Response(400, json={"error": {"message": "no such model"}})
        )

        async with _client() as client:
            with pytest.raises(LLMRequestFailed, match="400 from the model provider.*no such"):
                await client.complete_json(PROMPT)


class TestRetries:
    @respx.mock
    async def test_a_429_is_retried_and_then_succeeds(self):
        route = respx.post(COMPLETIONS).mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "0"}),
                httpx.Response(200, json=_completion("{}")),
            ]
        )

        async with _client(policy=RetryPolicy(max_retries=3, initial_backoff=0.0)) as client:
            assert (await client.complete_json(PROMPT)).parsed == {}
        assert route.call_count == 2

    @respx.mock
    async def test_a_400_is_not_retried(self):
        """A malformed request, a bad credential or a missing model fails
        identically four times and is better raised at once."""
        route = respx.post(COMPLETIONS).mock(return_value=httpx.Response(400, json={}))

        async with _client(policy=RetryPolicy(max_retries=3, initial_backoff=0.0)) as client:
            with pytest.raises(LLMRequestFailed):
                await client.complete_json(PROMPT)
        assert route.call_count == 1

    @respx.mock
    async def test_an_exhausted_5xx_still_arrives_as_a_request_failure(self):
        route = respx.post(COMPLETIONS).mock(return_value=httpx.Response(503))

        async with _client(policy=RetryPolicy(max_retries=2, initial_backoff=0.0)) as client:
            with pytest.raises(LLMRequestFailed, match="503"):
                await client.complete_json(PROMPT)
        assert route.call_count == 3

    @respx.mock
    async def test_every_attempt_spends_the_request_budget(self):
        """A retry is a request upstream sees, so it is a request the budget has
        to see too — which is why the limiter is acquired inside the retried
        operation rather than once per logical call."""
        spent = []

        class _CountingLimiter:
            async def acquire(self, n: int = 1) -> None:
                spent.append(n)

        respx.post(COMPLETIONS).mock(
            side_effect=[
                httpx.Response(500),
                httpx.Response(200, json=_completion("{}")),
            ]
        )

        async with _client(
            limiter=_CountingLimiter(), policy=RetryPolicy(max_retries=1, initial_backoff=0.0)
        ) as client:
            await client.complete_json(PROMPT)

        assert spent == [1, 1]


class TestTheSharedBudget:
    def test_the_client_shares_the_deepinfra_budget_by_default(self):
        """One bucket for the whole app (ADR-0006), reached by source name.

        The identity check *is* the assertion: `get_rate_limiter` is cached on
        (source, budget), so matching here proves the client asked for exactly
        this budget rather than a private limiter that would let a second
        process call the provider at twice the configured rate — the rake this
        codebase has already stepped on twice (NEU-955, then ADR-0006).
        """
        client = _client(limiter=None)
        assert client._limiter is get_rate_limiter("deepinfra", Budget(5, 1))

    def test_an_explicit_limiter_still_wins(self):
        """`limiter=` is the opt-out an isolated caller needs — every unit test
        here takes it, which is why none of them needs a database."""
        isolated = RateLimiter(20, 1)
        assert _client(limiter=isolated)._limiter is isolated

    def test_the_budget_takes_no_lease(self):
        """Leasing amortises lock traffic on a source hit tens of times a second.
        This one is called once per changed user per week, so a locked round trip
        per request is free — do not "unify" it upward with TMDB's."""
        assert _budget(5, 1.0).lease == 1


class TestPromptAndConstruction:
    @respx.mock
    async def test_an_instruction_without_the_word_json_is_refused_before_any_request(self):
        """**Measured, not assumed**: the provider answers 400 when
        `response_format` is set and the word appears nowhere. Enforced rather
        than commented so nobody "cleaning up" the wording can break it."""
        route = respx.post(COMPLETIONS).mock(
            return_value=httpx.Response(200, json=_completion("{}"))
        )

        async with _client() as client:
            with pytest.raises(UnsupportedPromptError, match='literal lower-case word "json"'):
                await client.complete_json(Prompt(system="Answer tersely.", user="hi"))
        assert route.call_count == 0

    @respx.mock
    async def test_the_match_is_case_sensitive_on_purpose(self):
        """Nobody measured whether the provider lowercases before looking, so
        accepting "JSON" on that assumption would wave through the exact 400 this
        guard exists to stop. The refusal is loud, local and one word to fix; the
        failure it replaces is a lost week for a user."""
        route = respx.post(COMPLETIONS).mock(
            return_value=httpx.Response(200, json=_completion("{}"))
        )

        async with _client() as client:
            with pytest.raises(UnsupportedPromptError):
                await client.complete_json(Prompt(system="Reply with JSON.", user="hi"))
        assert route.call_count == 0

    @respx.mock
    async def test_the_word_in_the_user_payload_does_not_satisfy_the_rule(self):
        """Stricter than the provider, which reads the whole prompt: the word
        belongs with the authored instruction, not incidentally inside a payload
        that varies per call."""
        route = respx.post(COMPLETIONS).mock(
            return_value=httpx.Response(200, json=_completion("{}"))
        )

        async with _client() as client:
            with pytest.raises(UnsupportedPromptError):
                await client.complete_json(Prompt(system="Answer tersely.", user="as json"))
        assert route.call_count == 0

    async def test_a_missing_api_key_raises_at_construction(self):
        with pytest.raises(ValueError, match="DEEPINFRA_API_KEY"):
            _client(api_key=None)

    async def test_a_missing_model_raises_at_construction(self):
        """Not defaulted on purpose: the id is measured rather than recalled,
        and one asserted from memory is a non-retryable 404 in prod."""
        with pytest.raises(ValueError, match="RECOMMENDATION_MODEL"):
            _client(model=None)

    async def test_an_unregistered_provider_raises_at_construction(self):
        """Before the job starts spending tokens, rather than at the first call."""
        with pytest.raises(KeyError):
            _client(provider="openai")
