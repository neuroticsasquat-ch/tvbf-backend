"""Send one Web Push message and classify what happened (NEU-1484, spec §5.2).

**This is the one module that imports `pywebpush`**, and tests mock at this
boundary: callers patch `send`, and this module's own tests patch the
`webpush` name below. `respx` is not the seam — the library builds and posts
the request itself, over `requests` (spec §8).

`pywebpush.webpush` is synchronous, so it runs in a worker thread; an event loop
blocked on a slow push service would stall every request in the process.

The outcome is a value, never an exception: the delivery job's rules are keyed
on it (`Sent` → mark the row sent; `Gone` → delete the subscription; `Failed` →
count towards the retirement limit), and a raise would push that dispatch into
every caller's `except` clauses.
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import urlsplit

from pywebpush import WebPushException, webpush
from requests import Response

from tvbf.config import get_settings

log = logging.getLogger(__name__)

# One day, the spec's TTL (§5.2): an airs-today alert that arrives tomorrow is
# wrong, and the push service drops it rather than delivering it late.
DEFAULT_TTL_SECONDS = 86400
# Bounds a hung push service. The library's default is no timeout at all, and
# the delivery job is sequential per subscription.
_TIMEOUT_SECONDS = 10.0
# 404 and 410 are the push service saying this subscription will never work
# again — unsubscribed, expired, or made against a rotated VAPID key.
_GONE_STATUSES = frozenset({404, 410})


class VapidNotConfigured(Exception):
    """`VAPID_PRIVATE_KEY`, `VAPID_PUBLIC_KEY` or `VAPID_SUBJECT` is unset.

    Callers are expected to check `Settings.vapid_configured` and refuse first;
    reaching this is a caller bug, so it raises rather than being an outcome.
    """


@dataclass(frozen=True)
class SubscriptionKeys:
    """What a push needs from an `app.push_subscription` row — the
    `PushSubscription.toJSON()` shape, flattened."""

    endpoint: str
    p256dh: str
    auth: str


@dataclass(frozen=True)
class Sent:
    status: int


@dataclass(frozen=True)
class Gone:
    status: int


@dataclass(frozen=True)
class Failed:
    """Any other non-2xx (`status` set) or a transport error (`status` None)."""

    status: int | None
    error: str


type SendOutcome = Sent | Gone | Failed


async def send(
    subscription: SubscriptionKeys,
    payload: dict[str, Any],
    *,
    ttl: int = DEFAULT_TTL_SECONDS,
) -> SendOutcome:
    """Encrypt `payload` as JSON, send it to `subscription`, and classify the result."""
    settings = get_settings()
    subject = settings.vapid_subject
    # The `None` check only narrows for the type checker; `vapid_configured` implies it.
    if not settings.vapid_configured or subject is None:
        raise VapidNotConfigured
    try:
        response = await asyncio.to_thread(
            webpush,
            subscription_info={
                "endpoint": subscription.endpoint,
                "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth},
            },
            data=json.dumps(payload),
            vapid_private_key=settings.vapid_private_key,
            # A fresh dict per call: `webpush` writes `aud` and `exp` into the
            # one it is given, and a shared one would carry the first
            # endpoint's audience to every later push service.
            vapid_claims={"sub": subject},
            ttl=ttl,
            headers={"Urgency": "normal"},
            timeout=_TIMEOUT_SECONDS,
        )
    except WebPushException as exc:
        # `webpush` raises for any status above 202, carrying the response — so
        # a 203-299 lands here as `Failed` rather than the spec's "2xx → sent".
        # Push services answer 201; nothing in that range is seen in practice.
        status = exc.status_code
        if status in _GONE_STATUSES:
            return Gone(status=status)
        return Failed(status=status, error=exc.message)
    except Exception as exc:  # requests' transport errors, or a malformed stored key
        # Broad on purpose: one bad subscription must not abort a delivery run,
        # and the job counts this towards retirement like any other failure.
        # The host only: the full endpoint is a capability URL.
        log.warning("push send raised host=%s", urlsplit(subscription.endpoint).hostname)
        return Failed(status=None, error=f"{type(exc).__name__}: {exc}")
    # `webpush` returns a curl command string only when `curl=True`.
    return Sent(status=cast(Response, response).status_code)
