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

from tvbf.llm.client import OpenAICompatClient
from tvbf.llm.registry import DEEPINFRA
from tvbf.llm.retry import RetryPolicy
from tvbf.llm.types import (
    LLMRequestFailed,
    LLMResponseInvalid,
    Prompt,
    UnsupportedPromptError,
)
from tvbf.rate_budget import RateLimiter

BASE = "https://api.deepinfra.com/v1/openai"
COMPLETIONS = f"{BASE}/chat/completions"

# The instruction has to carry the literal word "json" or the provider 400s.
PROMPT = Prompt(system="Answer as a json object.", user="what should I watch?")


def _client(**overrides) -> OpenAICompatClient:
    kwargs = {
        "provider": DEEPINFRA,
        "api_key": "test-key",
        "model": "deepseek-ai/Some-Model",
        # An isolated in-process limiter, so no unit test needs a database. Once
        # NEU-1099 registers the `deepinfra` bucket this is also what keeps the
        # shared budget out of the unit suite.
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
            with pytest.raises(LLMResponseInvalid, match="envelope did not decode"):
                await client.complete_json(PROMPT)


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
            with pytest.raises(UnsupportedPromptError, match='literal word "json"'):
                await client.complete_json(Prompt(system="Answer tersely.", user="hi"))
        assert route.call_count == 0

    @respx.mock
    async def test_the_word_json_is_matched_case_insensitively(self):
        """A request is attempted, which is the whole assertion: the guard let an
        upper-case spelling through. The transport failure is only how this test
        avoids needing a plausible response body."""
        route = respx.post(COMPLETIONS).mock(side_effect=httpx.ConnectError("nope"))

        async with _client() as client:
            with pytest.raises(LLMRequestFailed):
                await client.complete_json(Prompt(system="Reply with JSON.", user="hi"))
        assert route.call_count == 1

    async def test_a_missing_api_key_raises_at_construction(self):
        with pytest.raises(ValueError, match="DEEPINFRA_API_KEY"):
            _client(api_key=None)

    async def test_a_missing_model_raises_at_construction(self):
        """Not defaulted on purpose: the exact DeepSeek id is NEU-1100's to
        measure, and one asserted from memory is a non-retryable 404 in prod."""
        with pytest.raises(ValueError, match="RECOMMENDATION_MODEL"):
            _client(model=None)

    async def test_an_unregistered_provider_raises_at_construction(self):
        """Before the job starts spending tokens, rather than at the first call."""
        with pytest.raises(KeyError):
            _client(provider="openai")
