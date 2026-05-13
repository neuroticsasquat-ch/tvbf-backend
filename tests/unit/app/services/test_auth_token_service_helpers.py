"""Pure-function unit tests for auth_token_service (no DB)."""

from datetime import timedelta

import pytest

from tvbf.app.services import auth_token_service as svc


def test_ttl_for_known_purposes() -> None:
    assert svc.ttl_for(svc.PURPOSE_PASSWORD_RESET) == timedelta(hours=1)
    assert svc.ttl_for(svc.PURPOSE_EMAIL_VERIFICATION) == timedelta(hours=24)


def test_ttl_for_unknown_purpose_raises() -> None:
    with pytest.raises(ValueError):
        svc.ttl_for("nope")


def test_hash_is_stable_and_sha256_hex() -> None:
    h1 = svc._hash("abc")
    h2 = svc._hash("abc")
    assert h1 == h2
    assert len(h1) == 64
    assert int(h1, 16) >= 0  # all hex chars
    assert svc._hash("abc") != svc._hash("abd")
