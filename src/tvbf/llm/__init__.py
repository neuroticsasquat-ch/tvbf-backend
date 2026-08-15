"""Asking a hosted model for JSON, and nothing about taste.

**A trimmed port of `upcoming-movies-backend/src/upmovies/llm/`** (NEU-1098).
That package is a mature provider-neutral gateway with its own ADR trail; what
transfers is the provider-neutral prompt type, the OpenAI-compatible client and
the retry policy. What does not: `pricing`, `offline`, the four-stage
configuration validation, the byte-identical-prefix caching contract, and the
Anthropic adapter — all earned by upmovies' four stages across three providers,
and inert for one stage calling one provider a few times a week.

**The copy is deliberate and its cost is accepted in writing**: the two codebases
will drift, and a fix in one will not reach the other (project spec §6). Each
module records what it took and what it left; see `client.py` for why a shared
package is the textbook answer and the wrong one here.

The module is `llm/`, not `deepinfra/`: the provider is a setting, so the module
is not named for it — the same reasoning that named `catalog` for what it holds
rather than where it came from. Nothing recommendation-specific lives here. This
knows how to ask a model for JSON; the taste payload (M2), storage and resolution
(M3) and the weekly pass (M4) are all elsewhere.
"""

from tvbf.llm.client import SOURCE as SOURCE
from tvbf.llm.client import OpenAICompatClient as OpenAICompatClient
from tvbf.llm.registry import DEEPINFRA as DEEPINFRA
from tvbf.llm.registry import base_url_for as base_url_for
from tvbf.llm.retry import DEFAULT_RETRY_POLICY as DEFAULT_RETRY_POLICY
from tvbf.llm.retry import RetryPolicy as RetryPolicy
from tvbf.llm.types import LLMError as LLMError
from tvbf.llm.types import LLMRequestFailed as LLMRequestFailed
from tvbf.llm.types import LLMResponse as LLMResponse
from tvbf.llm.types import LLMResponseInvalid as LLMResponseInvalid
from tvbf.llm.types import Prompt as Prompt
from tvbf.llm.types import UnsupportedPromptError as UnsupportedPromptError
