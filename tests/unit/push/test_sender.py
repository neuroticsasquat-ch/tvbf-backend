"""`push.sender.send` — the three outcomes, with `webpush` mocked at the module
boundary (NEU-1484, spec §8)."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import requests
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from py_vapid.utils import b64urlencode
from pywebpush import WebPushException

from tvbf.config import get_settings
from tvbf.push import sender
from tvbf.push.keys import generate
from tvbf.push.sender import (
    Failed,
    Gone,
    Sent,
    SubscriptionKeys,
    VapidNotConfigured,
    send,
)

_SUB = SubscriptionKeys(
    endpoint="https://fcm.googleapis.com/fcm/send/abc123", p256dh="p256", auth="authsecret"
)


@pytest.fixture
def vapid(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "vapid_private_key", "priv")
    monkeypatch.setattr(settings, "vapid_public_key", "pub")
    monkeypatch.setattr(settings, "vapid_subject", "mailto:ops@example.com")
    return settings


@pytest.fixture
def webpush(monkeypatch):
    mock = MagicMock(return_value=SimpleNamespace(status_code=201))
    monkeypatch.setattr(sender, "webpush", mock)
    return mock


def _rejection(status: int) -> WebPushException:
    return WebPushException(f"Push failed: {status}", response=SimpleNamespace(status_code=status))


async def test_2xx_is_sent_with_its_status(vapid, webpush):
    assert await send(_SUB, {"kind": "test"}) == Sent(status=201)


async def test_the_request_carries_the_subscription_payload_vapid_and_ttl(vapid, webpush):
    await send(_SUB, {"kind": "test", "title": "Hi"}, ttl=60)
    kwargs = webpush.call_args.kwargs
    assert kwargs["subscription_info"] == {
        "endpoint": _SUB.endpoint,
        "keys": {"p256dh": "p256", "auth": "authsecret"},
    }
    assert json.loads(kwargs["data"]) == {"kind": "test", "title": "Hi"}
    assert kwargs["vapid_private_key"] == "priv"
    assert kwargs["vapid_claims"] == {"sub": "mailto:ops@example.com"}
    assert kwargs["ttl"] == 60
    assert kwargs["headers"] == {"Urgency": "normal"}
    assert kwargs["timeout"] is not None


async def test_ttl_defaults_to_one_day(vapid, webpush):
    await send(_SUB, {})
    assert webpush.call_args.kwargs["ttl"] == 86400


async def test_claims_are_a_fresh_dict_each_call(vapid, webpush):
    # `webpush` writes `aud` into the claims it is given.
    await send(_SUB, {})
    webpush.call_args.kwargs["vapid_claims"]["aud"] = "https://fcm.googleapis.com"
    await send(_SUB, {})
    assert webpush.call_args.kwargs["vapid_claims"] == {"sub": "mailto:ops@example.com"}


@pytest.mark.parametrize("status", [404, 410])
async def test_404_and_410_are_gone(vapid, webpush, status):
    webpush.side_effect = _rejection(status)
    assert await send(_SUB, {}) == Gone(status=status)


@pytest.mark.parametrize("status", [400, 413, 429, 500, 503])
async def test_other_rejections_fail_with_their_status(vapid, webpush, status):
    webpush.side_effect = _rejection(status)
    outcome = await send(_SUB, {})
    assert isinstance(outcome, Failed)
    assert outcome.status == status
    assert str(status) in outcome.error


async def test_a_rejection_without_a_response_fails_with_no_status(vapid, webpush):
    webpush.side_effect = WebPushException("VAPID dict missing 'private_key'")
    assert await send(_SUB, {}) == Failed(status=None, error="VAPID dict missing 'private_key'")


async def test_a_transport_error_fails_with_no_status(vapid, webpush):
    webpush.side_effect = requests.ConnectionError("connection refused")
    outcome = await send(_SUB, {})
    assert isinstance(outcome, Failed)
    assert outcome.status is None
    assert "ConnectionError" in outcome.error


async def test_a_transport_error_logs_the_host_not_the_endpoint(vapid, webpush, caplog):
    webpush.side_effect = requests.Timeout("timed out")
    with caplog.at_level("WARNING"):
        await send(_SUB, {})
    assert "fcm.googleapis.com" in caplog.text
    assert "abc123" not in caplog.text


@pytest.mark.parametrize("unset", ["vapid_private_key", "vapid_public_key", "vapid_subject"])
async def test_refuses_when_vapid_is_not_configured(vapid, webpush, monkeypatch, unset):
    monkeypatch.setattr(vapid, unset, None)
    with pytest.raises(VapidNotConfigured):
        await send(_SUB, {})
    webpush.assert_not_called()


async def test_a_real_key_signs_and_posts(vapid, monkeypatch):
    """Unmocked down to the HTTP post: the generated key format is one `webpush`
    accepts, and the request it builds carries our TTL and urgency."""
    keys = generate("mailto:ops@example.com")
    monkeypatch.setattr(vapid, "vapid_private_key", keys["VAPID_PRIVATE_KEY"])
    monkeypatch.setattr(vapid, "vapid_public_key", keys["VAPID_PUBLIC_KEY"])
    # A browser-side key pair to encrypt to, as `PushSubscription` would carry.
    browser_key = ec.generate_private_key(ec.SECP256R1())
    p256dh = b64urlencode(
        browser_key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    )
    posted = {}

    def fake_post(url, data=None, headers=None, timeout=None):
        posted.update(url=url, headers=headers)
        return SimpleNamespace(status_code=201, reason="Created", text="", headers={})

    monkeypatch.setattr(requests, "post", fake_post)
    outcome = await send(SubscriptionKeys(_SUB.endpoint, p256dh, b64urlencode(b"0" * 16)), {})
    assert outcome == Sent(status=201)
    assert posted["url"] == _SUB.endpoint
    assert posted["headers"]["ttl"] == "86400"
    assert posted["headers"]["Urgency"] == "normal"
    assert posted["headers"]["Authorization"].startswith("vapid t=")
