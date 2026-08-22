"""`client_ip` — the trust rule for the address a throttle is keyed on (NEU-1160)."""

from fastapi import Request

from tvbf.client_ip import client_ip


def _request(*, headers: dict[str, str] | None = None, peer: str | None = "10.0.0.1") -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope: dict = {
        "type": "http",
        "method": "POST",
        "path": "/auth/signup",
        "headers": raw,
    }
    if peer is not None:
        scope["client"] = (peer, 4321)
    return Request(scope)


def test_takes_the_right_most_entry_at_one_hop():
    req = _request(headers={"X-Forwarded-For": "1.2.3.4, 9.9.9.9"})
    assert client_ip(req, trusted_proxy_hops=1) == "9.9.9.9"


def test_a_forged_left_most_entry_is_ignored():
    # The attacker authors everything left of what the proxy appended.
    req = _request(headers={"X-Forwarded-For": "203.0.113.7"})
    assert client_ip(req, trusted_proxy_hops=1) == "203.0.113.7"

    forged = _request(headers={"X-Forwarded-For": "198.51.100.1, 203.0.113.7"})
    assert client_ip(forged, trusted_proxy_hops=1) == "203.0.113.7"


def test_two_hops_moves_the_boundary_one_entry_left():
    req = _request(headers={"X-Forwarded-For": "1.2.3.4, 5.6.7.8, 9.9.9.9"})
    assert client_ip(req, trusted_proxy_hops=2) == "5.6.7.8"


def test_falls_back_to_the_peer_with_no_header():
    assert client_ip(_request(), trusted_proxy_hops=1) == "10.0.0.1"


def test_falls_back_to_the_peer_when_the_header_holds_too_few_entries():
    req = _request(headers={"X-Forwarded-For": "9.9.9.9"})
    assert client_ip(req, trusted_proxy_hops=3) == "10.0.0.1"


def test_empty_entries_are_dropped_before_counting():
    req = _request(headers={"X-Forwarded-For": "1.2.3.4, , 9.9.9.9,"})
    assert client_ip(req, trusted_proxy_hops=1) == "9.9.9.9"


def test_a_non_ip_value_resolves_to_none():
    # app.auth_attempt.ip is INET; a junk value would raise DataError at insert.
    req = _request(headers={"X-Forwarded-For": "not-an-ip"})
    assert client_ip(req, trusted_proxy_hops=1) is None


def test_an_empty_header_falls_back_to_the_peer():
    req = _request(headers={"X-Forwarded-For": ""})
    assert client_ip(req, trusted_proxy_hops=1) == "10.0.0.1"


def test_no_header_and_no_peer_is_none():
    assert client_ip(_request(peer=None), trusted_proxy_hops=1) is None


def test_ipv6_is_accepted_verbatim():
    req = _request(headers={"X-Forwarded-For": "2001:db8::1"})
    assert client_ip(req, trusted_proxy_hops=1) == "2001:db8::1"
