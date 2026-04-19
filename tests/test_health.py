from fastapi.testclient import TestClient

from tvbf.main import app

client = TestClient(app)


def test_healthz_returns_200_and_ok_body():
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_returns_200_and_ok_body():
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
