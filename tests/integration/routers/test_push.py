"""GET /push/vapid-public-key — unauthenticated, publicly cacheable (NEU-1484)."""

import pytest
from httpx import ASGITransport, AsyncClient

from tvbf.config import get_settings
from tvbf.main import app


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        yield c


@pytest.fixture
def vapid(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "vapid_private_key", "priv")
    monkeypatch.setattr(settings, "vapid_public_key", "BPubKey")
    monkeypatch.setattr(settings, "vapid_subject", "mailto:ops@example.com")
    return settings


async def test_returns_the_public_key_without_a_session(client, vapid):
    r = await client.get("/push/vapid-public-key")
    assert r.status_code == 200, r.text
    assert r.json() == {"public_key": "BPubKey"}


async def test_is_publicly_cacheable_for_a_day(client, vapid):
    r = await client.get("/push/vapid-public-key")
    assert r.headers["cache-control"] == "public, max-age=86400"


@pytest.mark.parametrize("unset", ["vapid_private_key", "vapid_public_key", "vapid_subject"])
async def test_503_when_any_vapid_value_is_unset(client, vapid, monkeypatch, unset):
    monkeypatch.setattr(vapid, unset, None)
    r = await client.get("/push/vapid-public-key")
    assert r.status_code == 503
    assert r.json() == {"detail": "vapid_not_configured"}
    assert "public" not in r.headers.get("cache-control", "")
