"""Which providers this module can reach, and where they live.

**Ported from `upcoming-movies-backend/src/upmovies/llm/registry.py`**
(NEU-1098), down to one entry.

**Base URLs are constants here, not settings** (project spec §6). A base URL is
a property of the provider, not of a deployment: making it configurable adds an
env var whose only correct value is the one written below, plus a second one to
get wrong. Tests reach the client by mocking the transport, not by pointing it
at another host. **The model id is** a setting (`RECOMMENDATION_MODEL`) — that
is the knob that actually gets turned.

One entry is still a registry rather than a bare constant, for the same reason
`rate_budget.BUCKETS` is: `base_url_for` raising on an unregistered provider is
what stops a typo becoming a request to a host nobody chose. The string is also
the budget key NEU-1099 registers in `rate_budget.BUCKETS`, so it is a named
constant rather than a literal repeated per call site.
"""

DEEPINFRA = "deepinfra"

PROVIDERS: tuple[str, ...] = (DEEPINFRA,)

_OPENAI_COMPAT_BASE_URLS: dict[str, str] = {
    DEEPINFRA: "https://api.deepinfra.com/v1/openai",
}


def base_url_for(provider: str) -> str:
    """Base URL of an OpenAI-compatible provider.

    Raises `KeyError` for anything else — a provider nobody wrote an entry for
    is a mistake worth crashing on, not one worth guessing an endpoint for.
    """
    return _OPENAI_COMPAT_BASE_URLS[provider]
