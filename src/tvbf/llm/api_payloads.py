"""Pydantic shapes for parsing DeepInfra's OpenAI-compatible chat-completions
response (NEU-1098).

Its own module because CLAUDE.md's vocabulary rule says so — **a new upstream gets
its own `api_payloads`** — and DeepInfra is one. The rule earns its keep here
rather than merely being obeyed: the same shapes that keep upstream parsing out of
`schemas.py` are what make a malformed envelope a *typed* failure. Reading the
envelope with `payload.get("choices")` chains parses a body that decoded to a list
or a string right up to an `AttributeError`, which is neither `LLMRequestFailed`
nor `LLMResponseInvalid` and so escapes the taxonomy M4's failure semantics
dispatch on. One `model_validate` closes that, including a `prompt_tokens` that
arrives as something other than a number.

Narrow on purpose, the way `airdates/api_payloads.py` is: four fields survive a
call, and nothing here models the streaming deltas, tool calls, log-probs or the
`prompt_tokens_details` cache split that the endpoint can also carry. upmovies
maps that split because its `pricing.py` must keep four token counts disjoint;
`pricing.py` is not ported, so the split has no reader (see `types.LLMResponse`).

No `OptionalDate` alias here, unlike both sibling parsers — this payload carries
no dates at all.
"""

from pydantic import BaseModel, ConfigDict


class _Upstream(BaseModel):
    """Ignore fields nobody here reads, rather than forbidding them: a provider
    adding a key is not a reason to fail a call that otherwise came back fine."""

    model_config = ConfigDict(extra="ignore")


class ChatMessage(_Upstream):
    """`content` is optional because the provider genuinely omits it — a reply cut
    off at `max_tokens` arrives with the key present and empty. `client` is what
    decides that is unusable, so this only has to represent it."""

    content: str | None = None


class ChatChoice(_Upstream):
    """`finish_reason` is kept for one reason: it is what makes an empty `content`
    actionable. `"length"` means the reply hit `max_tokens` and the fix is a
    higher ceiling; anything else means the model produced nothing and the fix is
    a different prompt or model. The two are indistinguishable from the text, and
    it matters more than it looks — DeepSeek is a reasoning model, so its
    reasoning tokens count against the ceiling while never appearing in what
    comes back."""

    message: ChatMessage | None = None
    finish_reason: str | None = None


class ChatUsage(_Upstream):
    """What the call cost, in the provider's own numbers.

    Defaulting to 0 rather than `None`: `LLMResponse` promises two ints and M4
    stores them directly, so optionality here would only move the same question
    downstream into a nullable column. A provider that omits the block entirely
    is logged by `client` instead — see `_usage_or_zero`."""

    prompt_tokens: int = 0
    completion_tokens: int = 0


class ChatCompletion(_Upstream):
    """One chat-completions response.

    `choices` defaults to empty and `usage` to `None` so that a *structurally*
    valid envelope missing either one reaches the client's own verdict, which can
    say which is missing. A body that is not an object at all fails validation
    here, which is the point of the module.
    """

    choices: list[ChatChoice] = []
    usage: ChatUsage | None = None
