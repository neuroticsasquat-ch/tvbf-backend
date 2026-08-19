"""The one place a request's client address is resolved.

Every request reaching this app has been through exactly one proxy — Traefik,
in both environments (Coolify's in prod, `tbc-localdev-infra`'s locally).
Uvicorn is started without `--proxy-headers`, so `request.client.host` is that
proxy for **every** request and is useless as a throttle key.

The rule is expressed in *hops from the right*, never in Traefik's
configuration. The left-most `X-Forwarded-For` entry is whatever the client
sent, which an attacker authors freely; the right-most is what the nearest
proxy observed:

    X-Forwarded-For: 1.2.3.4, 9.9.9.9
                     ^^^^^^^ forged by the client
                              ^^^^^^^ appended by Traefik — the peer it saw
    hops=1 -> 9.9.9.9

That holds whether the proxy appends to an incoming header or replaces it
outright, which is why hops is the knob. **Raising `TRUSTED_PROXY_HOPS` is a
trust decision**: each increment moves the trusted boundary one entry left, and
setting it higher than the number of proxies actually in front of the app hands
the key straight to the client.
"""

import ipaddress

from fastapi import Request


def client_ip(request: Request, *, trusted_proxy_hops: int) -> str | None:
    """The address to key a throttle on, or `None` when there isn't a trustworthy one.

    `None` means the caller does not throttle this request. That is the correct
    failure: absent a trustworthy address the alternatives are throttling
    everybody against one global counter (trivially weaponised into a denial of
    service against every user) or refusing the request outright (a
    self-inflicted outage the first time a proxy header changes shape).

    The value is validated with `ipaddress.ip_address` because
    `app.auth_attempt.ip` is `INET`: a non-IP string raises `DataError` at
    insert and 500s the request.

    IPv6 is deliberately **not** folded to a /64. Keying on a bare IPv6 address
    is normally a hole — a residential allocation is a /64 and an attacker
    rotates inside it for free — but `api.tvbingefriend.com` has an `A` record
    and no `AAAA` record (checked 2026-08-19), so no request arrives over IPv6
    and the hole is unreachable. The day an `AAAA` record is added, fold to the
    /64 here before `app.auth_attempt` means anything; nothing else will say so.
    """
    candidate: str | None = None

    forwarded = request.headers.get("x-forwarded-for")
    if forwarded and trusted_proxy_hops >= 1:
        entries = [e for e in (part.strip() for part in forwarded.split(",")) if e]
        if len(entries) >= trusted_proxy_hops:
            candidate = entries[-trusted_proxy_hops]

    if candidate is None:
        candidate = request.client.host if request.client else None
    if candidate is None:
        return None

    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return None
    return candidate
