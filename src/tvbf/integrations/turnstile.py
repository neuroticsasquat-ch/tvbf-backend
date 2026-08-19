"""Cloudflare Turnstile token verification (NEU-1160).

Beside `integrations/linear.py`, which is this repo's precedent for an outbound
call made inside a request path.

**ADR-0002 is not violated.** That ADR forbids calling an upstream API in a live
request path, and what it forbids is fetching *catalog data* on a cache miss —
so that load scales with our traffic instead of with our catalog. A captcha
verification mirrors nothing and cannot be precomputed: the token is minted per
attempt and is worthless a second time. `POST /me/feedback` already calls Linear
inline on the same reading.

**Fail closed.** An unverifiable token means no account. The availability cost is
close to zero: `app.tvbingefriend.com` is Cloudflare-proxied, so during a
Cloudflare incident the SPA that renders the signup form is unreachable anyway.
Failing open would open the exact hole this module closes, in the one window an
attacker would most like to have it, and would do so invisibly.
"""

import logging

import httpx
from pydantic import BaseModel, ConfigDict, Field

log = logging.getLogger(__name__)

# A base URL is a property of the provider, not of a deployment — the rule
# `llm/registry.py` states and `config.py` repeats for the DeepInfra base URL.
_SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
_TIMEOUT_SECONDS = 5.0


class TurnstileRejected(Exception):
    """Cloudflare answered `success: false` — the token is expired, replayed or fake."""


class TurnstileUnavailable(Exception):
    """We could not get an answer out of Cloudflare: timeout, transport error,
    non-2xx, or a body that is not the documented shape."""


class SiteverifyResponse(BaseModel):
    """The siteverify envelope, read through a model rather than
    `payload.get("success")`.

    A body that decodes to a *list* — which is what an interception page or a
    misrouted response looks like — makes `.get()` raise `AttributeError`, which
    is neither exception above and escapes the handler as a 500. Same failure
    `llm/client.py` reads its envelope through `api_payloads.ChatCompletion` to
    avoid.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    success: bool
    error_codes: list[str] = Field(default_factory=list, alias="error-codes")


async def verify(*, token: str, secret: str, remote_ip: str | None) -> None:
    """Return normally when Cloudflare accepts the token; raise otherwise.

    `remoteip` is sent when we have one — Cloudflare uses it as a corroborating
    signal, so the two halves of NEU-1160 feed each other.
    """
    form = {"secret": secret, "response": token}
    if remote_ip is not None:
        form["remoteip"] = remote_ip

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as http:
            res = await http.post(_SITEVERIFY_URL, data=form)
    except httpx.HTTPError as exc:
        raise TurnstileUnavailable(f"transport error: {exc}") from exc

    if res.status_code // 100 != 2:
        raise TurnstileUnavailable(f"siteverify returned http {res.status_code}")

    try:
        parsed = SiteverifyResponse.model_validate(res.json())
    except Exception as exc:  # ValueError from json(), ValidationError from pydantic
        raise TurnstileUnavailable(f"unparseable siteverify body: {exc}") from exc

    if not parsed.success:
        # Logged, never returned: `error-codes` distinguishes an expired token
        # from a bad secret, and the second is our misconfiguration rather than
        # the user's problem.
        log.warning("turnstile rejected a token error_codes=%s", parsed.error_codes)
        raise TurnstileRejected(", ".join(parsed.error_codes) or "success=false")
