import httpx
from httpx import ASGITransport

from tvbf.main import app


async def test_cors_allows_configured_origin():
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.options(
            "/healthz",
            headers={
                "Origin": "https://tvbf.localhost",
                "Access-Control-Request-Method": "GET",
            },
        )
    assert r.status_code in (200, 204)
    assert r.headers.get("access-control-allow-origin") == "https://tvbf.localhost"


async def test_cors_blocks_unknown_origin():
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.options(
            "/healthz",
            headers={
                "Origin": "https://attacker.example.com",
                "Access-Control-Request-Method": "GET",
            },
        )
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers.keys()}


async def test_browse_response_has_cache_control_header():
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/genres")
    assert r.status_code == 200
    assert r.headers.get("cache-control") == "public, max-age=300"
