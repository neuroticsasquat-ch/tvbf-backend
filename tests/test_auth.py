from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from tvbf.deps import require_admin


def build_client(monkeypatch) -> TestClient:
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
    client = build_client(monkeypatch)
    r = client.get("/secret")
    assert r.status_code == 401


def test_require_admin_rejects_wrong_token(monkeypatch):
    client = build_client(monkeypatch)
    r = client.get("/secret", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_require_admin_accepts_correct_token(monkeypatch):
    client = build_client(monkeypatch)
    r = client.get("/secret", headers={"Authorization": "Bearer shh"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}
