"""Provider-neutral LLM types: what a request is, what a call returns, and how a
call fails.

**Ported from `upcoming-movies-backend/src/upmovies/llm/types.py`** (NEU-1098),
trimmed hard. What survives is the idea that a prompt is described by what it
requires rather than by how a vendor provides it. What does not: the
byte-identical-prefix caching contract, `prefill`, `Usage`'s four disjoint
counts, `CallResult`/`CallLog` telemetry and the `Completer`/`StageGateway`
protocols. All of those are earned by upmovies' four stages across three
providers; here there is one stage, one provider and one call a week.

Deliberately free of DB, SQLAlchemy, `httpx` and vendor imports — this is the
vocabulary the job talks in, and anything wire-shaped belongs in `client.py`.
"""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Prompt:
    """One request: the instruction that does not vary, the payload that does.

    `system` and `user` are named for what they carry rather than for a vendor's
    field, the way upmovies' `stable_prefix`/`user` pair is — but without its
    caching promise. That contract (stable content first, byte-identical across
    calls, so a provider's automatic prefix cache can engage) is deliberately
    not ported: one call per changed user per week shares no prefix with
    anything, so there is nothing for a cache to hit and a promise nobody can
    keep is worse than no promise.

    **The instruction must contain the literal word "json".** Measured by
    upmovies against the live endpoint and recorded in its `llm/types.py`:
    DeepSeek answers `400` when `response_format` is set and the word appears
    nowhere in the prompt. Every call this module makes sets `response_format`
    (see `client.complete_json`), so the constraint is unconditional here and
    `client._to_wire` enforces it rather than trusting a comment — nobody
    "cleaning up" the wording later can get past it. NEU-1100 re-measures the
    behaviour against DeepInfra's live catalog.

    `max_tokens` needs headroom beyond the visible answer: DeepSeek is a
    reasoning model and its reasoning tokens count against this ceiling while
    being absent from the text that comes back.
    """

    system: str
    user: str
    max_tokens: int = 4096


@dataclass(frozen=True)
class LLMResponse:
    """One completed call: what it said, and what it cost.

    `parsed` is a **JSON object**, never an array or a scalar. That is not a
    convenience: `response_format: {"type": "json_object"}` makes the provider
    force a top-level object, and the output contract (project spec §7) is one.
    A body that decodes to anything else is `LLMResponseInvalid`, which the job
    retries once, rather than a shape the caller has to re-check.

    `text` is kept beside it because the job stores the raw response verbatim —
    an unresolvable title is only diagnosable from what the model actually said.

    `input_tokens` / `output_tokens` are `prompt_tokens` / `completion_tokens`
    as the provider reported them, stored by M4 directly and never derived
    later. Note the deliberate divergence from upmovies, which subtracts cached
    prompt tokens out of `input_tokens` to keep its four counts disjoint so
    `price()` cannot charge them twice. `pricing.py` is not ported, so there is
    nothing to double-charge, and subtracting would leave a number that
    understates the prompt actually sent.
    """

    parsed: dict[str, Any]
    text: str
    input_tokens: int
    output_tokens: int


class LLMError(Exception):
    """Base for every way a call can fail. One base, deliberately: the job
    dispatches on the two subclasses below and nothing else."""


class LLMRequestFailed(LLMError):
    """The call did not come back with a usable HTTP response — transport error,
    timeout, or a status the client gave up on.

    **The job must not retry this.** The client has already retried whatever was
    worth retrying (`retry.py`), so a failure that reaches the caller has
    survived the backoff curve; asking again immediately buys the same failure
    and spends a user's turn in the weekly pass on it.
    """


class LLMResponseInvalid(LLMError):
    """A response arrived and could not be believed — the body did not decode as
    JSON, or decoded to something other than an object.

    **The job retries this exactly once**, against the same payload. Malformed
    model JSON is frequently a one-off and a second call costs a fraction of a
    cent; a second failure is recorded as a failed run for that user.
    """


class UnsupportedPromptError(ValueError):
    """A `Prompt` asks for something this provider cannot serve.

    A `ValueError` and deliberately outside `LLMError`: nothing was sent, so
    there is no call outcome to classify, and the taxonomy above is what the
    job's failure semantics are written against. Same reasoning as the class of
    this name in upmovies, which raises rather than degrading the request —
    a silently dropped field returns a well-formed answer to a question nobody
    asked.
    """
