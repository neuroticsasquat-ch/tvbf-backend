from collections.abc import AsyncIterator

import httpx
from fastapi.testclient import TestClient
from httpx import ASGITransport

from tvbf.deps import get_session
from tvbf.main import app

client = TestClient(app)


def test_healthz_returns_200_and_ok_body():
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readyz_returns_200_when_db_reachable(session):
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/readyz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


async def test_readyz_returns_503_when_db_unreachable():
    async def broken_session() -> AsyncIterator[object]:
        class _Broken:
            async def execute(self, *_a, **_kw):
                raise RuntimeError("simulated db outage")

        yield _Broken()

    app.dependency_overrides[get_session] = broken_session
    try:
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/readyz")
    finally:
        app.dependency_overrides.pop(get_session, None)
    assert r.status_code == 503
    assert "database not reachable" in r.json()["detail"]
