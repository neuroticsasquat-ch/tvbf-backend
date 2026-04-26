"""Unit tests for FastAPI dependencies that do NOT require a real DB.

- require_admin (bearer-token guard) — from test_auth.py
- require_csrf (CSRF guard) — from test_csrf.py
- get_session yields an AsyncSession — no DB tables required
"""

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from tvbf.db import SessionLocal
from tvbf.deps import get_session, require_admin, require_csrf

# ---------------------------------------------------------------------------
# require_admin (test_auth.py)
# ---------------------------------------------------------------------------


def build_admin_client(monkeypatch) -> TestClient:
    monkeypatch.setenv("ADMIN_TOKEN", "shh")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x/y")
    from tvbf.config import get_settings

    get_settings.cache_clear()

    app = FastAPI()

    @app.get("/secret", dependencies=[Depends(require_admin)])
    async def secret():
        return {"ok": True}

    return TestClient(app)


def test_require_admin_rejects_missing_header(monkeypatch):
    client = build_admin_client(monkeypatch)
    r = client.get("/secret")
    assert r.status_code == 401


def test_require_admin_rejects_wrong_token(monkeypatch):
    client = build_admin_client(monkeypatch)
    r = client.get("/secret", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_require_admin_accepts_correct_token(monkeypatch):
    client = build_admin_client(monkeypatch)
    r = client.get("/secret", headers={"Authorization": "Bearer shh"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}


# ---------------------------------------------------------------------------
# require_csrf (test_csrf.py)
# ---------------------------------------------------------------------------


def _build_csrf_app() -> FastAPI:
    app = FastAPI()
    router = APIRouter()

    @router.post("/danger", dependencies=[Depends(require_csrf)])
    async def danger() -> dict[str, bool]:
        return {"ok": True}

    @router.get("/safe")
    async def safe() -> dict[str, bool]:
        return {"ok": True}

    app.include_router(router)
    return app


@pytest.mark.asyncio
async def test_csrf_passes_when_header_matches_cookie():
    app = _build_csrf_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post(
            "/danger",
            headers={"X-CSRF-Token": "abc123", "Cookie": "csrf_token=abc123"},
        )
        assert r.status_code == 200


@pytest.mark.asyncio
async def test_csrf_rejects_when_header_missing():
    app = _build_csrf_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/danger", headers={"Cookie": "csrf_token=abc123"})
        assert r.status_code == 403
        assert r.json()["detail"] == "csrf_invalid"


@pytest.mark.asyncio
async def test_csrf_rejects_when_header_mismatched():
    app = _build_csrf_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post(
            "/danger",
            headers={"X-CSRF-Token": "different", "Cookie": "csrf_token=abc123"},
        )
        assert r.status_code == 403


@pytest.mark.asyncio
async def test_csrf_rejects_when_cookie_missing():
    app = _build_csrf_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/danger", headers={"X-CSRF-Token": "abc123"})
        assert r.status_code == 403


@pytest.mark.asyncio
async def test_get_does_not_require_csrf():
    app = _build_csrf_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/safe")
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# get_session — no DB tables required, just SessionLocal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_session_yields_an_async_session():
    gen = get_session()
    sess = await anext(gen)
    try:
        assert sess is not None
        # The generator yields a working AsyncSession from SessionLocal.
        async with SessionLocal() as direct:
            assert type(direct) is type(sess)
    finally:
        # Drain the generator so the underlying context manager closes.
        with pytest.raises(StopAsyncIteration):
            await anext(gen)
