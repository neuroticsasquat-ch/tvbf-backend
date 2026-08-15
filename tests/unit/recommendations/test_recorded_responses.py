"""The committed recordings, replayed through the real client (NEU-1107).

`tests/fixtures/recommendations/README.md` says where they came from. The point
of committing recordings rather than writing fixtures is that a hand-written
fixture encodes what we *think* the model returns (project spec §12) — so these
tests replay them through `OpenAICompatClient` exactly as the provider sent them,
`usage` block and all, and assert what the client does with each.

`respx` throughout: **no test ever calls DeepInfra.** The recordings are the only
thing here that ever did.
"""

import httpx
import pytest
import respx

from tests.fixtures import recommendations as recorded
from tvbf.llm.client import OpenAICompatClient
from tvbf.llm.registry import DEEPINFRA
from tvbf.llm.retry import RetryPolicy
from tvbf.llm.types import LLMResponseInvalid, Prompt
from tvbf.rate_budget import RateLimiter

COMPLETIONS = "https://api.deepinfra.com/v1/openai/chat/completions"
PROMPT = Prompt(system="Answer as a json object.", user="payload")


def _client() -> OpenAICompatClient:
    return OpenAICompatClient(
        provider=DEEPINFRA,
        api_key="test-key",
        model="deepseek-ai/Some-Model",
        rate_calls=5,
        rate_window=1,
        limiter=RateLimiter(20, 1),
        policy=RetryPolicy(max_retries=0),
    )


async def _replay(name: str):
    respx.post(COMPLETIONS).mock(return_value=httpx.Response(200, json=recorded.envelope(name)))
    async with _client() as client:
        return await client.complete_json(PROMPT)


@respx.mock
async def test_a_recorded_answer_parses_as_the_output_contract():
    response = await _replay(recorded.CLEAN)

    entries = response.parsed["recommendations"]
    assert len(entries) == 25
    # Every field §7 names, on every entry: `release_year` in particular, because
    # a recommendation without one is dropped and resolution has no other
    # disambiguator.
    assert all({"title", "release_year", "reason"} <= entry.keys() for entry in entries)
    assert all(isinstance(entry["release_year"], int) for entry in entries)


@respx.mock
async def test_the_recorded_token_counts_are_the_providers_own():
    """M4 stores these directly, so a recording is the only place their real
    shape — including DeepInfra's extra `estimated_cost` and
    `prompt_tokens_details` keys — is exercised at all."""
    response = await _replay(recorded.CLEAN)

    usage = recorded.envelope(recorded.CLEAN)["usage"]
    assert (response.input_tokens, response.output_tokens) == (
        usage["prompt_tokens"],
        usage["completion_tokens"],
    )


@respx.mock
async def test_a_reasoning_models_answer_is_rejected_rather_than_parsed():
    """The recording nobody would have thought to write: HTTP 200,
    `finish_reason: "stop"`, JSON mode requested and honoured as far as the
    provider is concerned — and a `<think>` block ahead of the JSON, so the
    content does not decode.

    `LLMResponseInvalid` is the half of the taxonomy the weekly pass retries
    exactly once. It matters that this is not `LLMRequestFailed`: the call
    succeeded, and asking again is worth one attempt."""
    with pytest.raises(LLMResponseInvalid):
        await _replay(recorded.MALFORMED)


def test_the_malformed_recording_really_is_a_reasoning_models_answer():
    """Pins what the file is, so a re-recording that lands a clean response in it
    fails here rather than quietly turning the test above into a no-op."""
    content = recorded.content(recorded.MALFORMED)

    assert content.lstrip().startswith("<think>")
    assert recorded.envelope(recorded.MALFORMED)["choices"][0]["finish_reason"] == "stop"
