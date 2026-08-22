"""NEU-1160 — Turnstile verification and the IP-keyed throttle on
`/auth/signup` and `/auth/login`.

Every request here sets `X-Forwarded-For` explicitly: `client_ip` reads the
right-most entry at the default one hop, so the header is how a test says which
address an attempt came from.
"""

import httpx
import pytest
import respx
from httpx import ASGITransport, AsyncClient

from tests.fixtures.handles import new_handle
from tvbf.config import get_settings
from tvbf.main import app, create_app

_SITEVERIFY = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


@pytest.fixture
async def client(session):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        yield c


@pytest.fixture
def turnstile_on():
    """Flip verification on for one test, as `test_feedback.py` flips the Linear
    flag — `get_settings` is `lru_cache`d, so the instance is shared."""
    settings = get_settings()
    prior_enabled = settings.turnstile_enabled
    prior_secret = settings.turnstile_secret_key
    settings.turnstile_enabled = True
    settings.turnstile_secret_key = "sek"
    try:
        yield settings
    finally:
        settings.turnstile_enabled = prior_enabled
        settings.turnstile_secret_key = prior_secret


def _signup_body(invite: str, *, email: str = "new@example.com", token: str | None = None):
    body = {
        "email": email,
        "password": "hunter2hunter2",
        "display_name": "New",
        "handle": new_handle(),
        "invite_code": invite,
    }
    if token is not None:
        body["turnstile_token"] = token
    return body


async def _user_count(session) -> int:
    from sqlalchemy import func, select

    from tvbf.app.models import User

    return (await session.execute(select(func.count()).select_from(User))).scalar_one()


# ---------------------------------------------------------------------------
# Turnstile (AC 1–5)
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_a_token_cloudflare_rejects_is_400_and_creates_no_user(
    client, session, make_invite, turnstile_on
):
    respx.post(_SITEVERIFY).mock(
        return_value=httpx.Response(200, json={"success": False, "error-codes": ["invalid-input"]})
    )
    invite = await make_invite()
    r = await client.post(
        "/auth/signup",
        json=_signup_body(invite, token="bad"),
        headers={"X-Forwarded-For": "9.9.9.1"},
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "captcha_invalid"
    assert await _user_count(session) == 0


@respx.mock
@pytest.mark.asyncio
async def test_no_token_while_enabled_is_400_captcha_required(
    client, session, make_invite, turnstile_on
):
    route = respx.post(_SITEVERIFY).mock(return_value=httpx.Response(200, json={"success": True}))
    invite = await make_invite()
    r = await client.post(
        "/auth/signup",
        json=_signup_body(invite),
        headers={"X-Forwarded-For": "9.9.9.2"},
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "captcha_required"
    # A missing token spends no outbound request.
    assert not route.called
    assert await _user_count(session) == 0


@respx.mock
@pytest.mark.asyncio
async def test_an_empty_token_while_enabled_is_400_captcha_required(
    client, session, make_invite, turnstile_on
):
    # §5's table says "token absent **or empty**" — `""` is a widget that
    # rendered and was never solved, which is the commonest of the two.
    route = respx.post(_SITEVERIFY).mock(return_value=httpx.Response(200, json={"success": True}))
    invite = await make_invite()
    r = await client.post(
        "/auth/signup",
        json=_signup_body(invite, token=""),
        headers={"X-Forwarded-For": "9.9.9.7"},
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "captcha_required"
    assert not route.called
    assert await _user_count(session) == 0


@respx.mock
@pytest.mark.asyncio
async def test_the_503_is_logged_at_warning_with_exc_info(
    client, make_invite, turnstile_on, caplog
):
    # §5.1: logged with `exc_info` so Sentry captures it.
    respx.post(_SITEVERIFY).mock(side_effect=httpx.ConnectError("no route"))
    invite = await make_invite()
    with caplog.at_level("WARNING", logger="tvbf.routers.auth"):
        r = await client.post(
            "/auth/signup",
            json=_signup_body(invite, token="tok"),
            headers={"X-Forwarded-For": "9.9.9.8"},
        )
    assert r.status_code == 503
    record = next(rec for rec in caplog.records if rec.name == "tvbf.routers.auth")
    assert record.levelname == "WARNING"
    assert record.exc_info is not None, "Sentry needs the exception, not just the message"


@respx.mock
@pytest.mark.asyncio
async def test_an_accepted_token_lets_signup_proceed(client, make_invite, turnstile_on):
    route = respx.post(_SITEVERIFY).mock(return_value=httpx.Response(200, json={"success": True}))
    invite = await make_invite()
    r = await client.post(
        "/auth/signup",
        json=_signup_body(invite, token="good"),
        headers={"X-Forwarded-For": "9.9.9.3"},
    )
    assert r.status_code == 201, r.text
    form = dict(httpx.QueryParams(route.calls[0].request.content.decode()))
    # The address from §2 is forwarded as a corroborating signal.
    assert form["remoteip"] == "9.9.9.3"


@respx.mock
@pytest.mark.asyncio
async def test_siteverify_timing_out_is_503_and_creates_no_user(
    client, session, make_invite, turnstile_on
):
    respx.post(_SITEVERIFY).mock(side_effect=httpx.ReadTimeout("slow"))
    invite = await make_invite()
    r = await client.post(
        "/auth/signup",
        json=_signup_body(invite, token="tok"),
        headers={"X-Forwarded-For": "9.9.9.4"},
    )
    assert r.status_code == 503
    assert r.json()["detail"] == "captcha_unavailable"
    assert await _user_count(session) == 0


@respx.mock
@pytest.mark.asyncio
async def test_siteverify_non_2xx_is_503(client, session, make_invite, turnstile_on):
    respx.post(_SITEVERIFY).mock(return_value=httpx.Response(500, text="boom"))
    invite = await make_invite()
    r = await client.post(
        "/auth/signup",
        json=_signup_body(invite, token="tok"),
        headers={"X-Forwarded-For": "9.9.9.5"},
    )
    assert r.status_code == 503
    assert await _user_count(session) == 0


@respx.mock
@pytest.mark.asyncio
async def test_disabled_makes_no_outbound_request_and_ignores_the_field(client, make_invite):
    # The default. respx would raise on any unmocked call, so a request here
    # would fail the test rather than reach the network.
    route = respx.post(_SITEVERIFY).mock(return_value=httpx.Response(200, json={"success": True}))
    invite = await make_invite()
    r = await client.post(
        "/auth/signup",
        json=_signup_body(invite, token="ignored"),
        headers={"X-Forwarded-For": "9.9.9.6"},
    )
    assert r.status_code == 201, r.text
    assert not route.called


def test_create_app_refuses_to_boot_enabled_without_a_secret(turnstile_on):
    turnstile_on.turnstile_secret_key = None
    with pytest.raises(RuntimeError, match="TURNSTILE_SECRET_KEY"):
        create_app()


# ---------------------------------------------------------------------------
# The IP throttle (AC 6–8, 11)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_sixth_signup_from_one_address_is_429(client, make_invite):
    for n in range(5):
        invite = await make_invite()
        r = await client.post(
            "/auth/signup",
            json=_signup_body(invite, email=f"burst{n}@example.com"),
            headers={"X-Forwarded-For": "203.0.113.9"},
        )
        assert r.status_code == 201, r.text

    invite = await make_invite()
    r = await client.post(
        "/auth/signup",
        json=_signup_body(invite, email="burst5@example.com"),
        headers={"X-Forwarded-For": "203.0.113.9"},
    )
    assert r.status_code == 429
    assert r.json()["detail"] == "rate_limited"
    assert r.headers["Retry-After"] == "3600"

    # A different address in the same window is unaffected.
    invite = await make_invite()
    other = await client.post(
        "/auth/signup",
        json=_signup_body(invite, email="elsewhere@example.com"),
        headers={"X-Forwarded-For": "203.0.113.10"},
    )
    assert other.status_code == 201, other.text


@pytest.mark.asyncio
async def test_a_rejected_signup_still_counts_against_the_address(client, make_invite):
    # Five duplicate-email attempts: account_service rolls back on EmailInUse,
    # which is exactly what the commit in auth_throttle.record stands in front of.
    invite = await make_invite()
    first = await client.post(
        "/auth/signup",
        json=_signup_body(invite, email="taken@example.com"),
        headers={"X-Forwarded-For": "203.0.113.11"},
    )
    assert first.status_code == 201
    for _ in range(4):
        invite = await make_invite()
        r = await client.post(
            "/auth/signup",
            json=_signup_body(invite, email="taken@example.com"),
            headers={"X-Forwarded-For": "203.0.113.11"},
        )
        assert r.status_code == 409

    invite = await make_invite()
    r = await client.post(
        "/auth/signup",
        json=_signup_body(invite, email="fresh@example.com"),
        headers={"X-Forwarded-For": "203.0.113.11"},
    )
    assert r.status_code == 429


@pytest.mark.asyncio
async def test_the_eleventh_failed_login_from_one_address_is_429(client, make_user):
    # A different email every time — the case the email-keyed lockout cannot see.
    for n in range(10):
        r = await client.post(
            "/auth/login",
            json={"email": f"stuff{n}@example.com", "password": "wrongwrongwrong"},
            headers={"X-Forwarded-For": "198.51.100.4"},
        )
        assert r.status_code == 401

    r = await client.post(
        "/auth/login",
        json={"email": "stuff10@example.com", "password": "wrongwrongwrong"},
        headers={"X-Forwarded-For": "198.51.100.4"},
    )
    assert r.status_code == 429
    assert r.json()["detail"] == "rate_limited"
    assert r.headers["Retry-After"] == "900"


@pytest.mark.asyncio
async def test_a_successful_login_does_not_clear_the_address_counter(client, make_user):
    await make_user(email="real@example.com", password="hunter2hunter2")
    for n in range(10):
        r = await client.post(
            "/auth/login",
            json={"email": f"nobody{n}@example.com", "password": "wrongwrongwrong"},
            headers={"X-Forwarded-For": "198.51.100.5"},
        )
        assert r.status_code == 401

    ok = await client.post(
        "/auth/login",
        json={"email": "real@example.com", "password": "hunter2hunter2"},
        headers={"X-Forwarded-For": "198.51.100.5"},
    )
    assert ok.status_code == 429, "an attacker's own valid account must not be a reset button"


@pytest.mark.asyncio
async def test_a_successful_login_is_not_counted(client, make_user):
    await make_user(email="clean@example.com", password="hunter2hunter2")
    for _ in range(11):
        r = await client.post(
            "/auth/login",
            json={"email": "clean@example.com", "password": "hunter2hunter2"},
            headers={"X-Forwarded-For": "198.51.100.6"},
        )
        assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_a_429_is_not_itself_recorded(client, session, make_user):
    from sqlalchemy import func, select

    from tvbf.app.models import AuthAttempt

    for n in range(12):
        await client.post(
            "/auth/login",
            json={"email": f"spray{n}@example.com", "password": "wrongwrongwrong"},
            headers={"X-Forwarded-For": "198.51.100.7"},
        )
    recorded = (await session.execute(select(func.count()).select_from(AuthAttempt))).scalar_one()
    assert recorded == 10, "rejections must not extend the ban"


@pytest.mark.asyncio
async def test_an_unresolvable_address_is_not_throttled_and_does_not_500(client, make_invite):
    # A non-IP forwarded value resolves to None: no key to count against, so the
    # request proceeds unthrottled rather than 500ing at the INET insert.
    for n in range(7):
        invite = await make_invite()
        r = await client.post(
            "/auth/signup",
            json=_signup_body(invite, email=f"nokey{n}@example.com"),
            headers={"X-Forwarded-For": "unknown"},
        )
        assert r.status_code == 201, r.text


@pytest.mark.asyncio
async def test_login_with_an_unresolvable_address_is_not_throttled_and_does_not_500(
    client, make_user
):
    # The login half of AC 11: both `enforce` and `record` take the None guard,
    # so a junk forwarded value must not reach the INET column.
    await make_user(email="nokey@example.com", password="hunter2hunter2")
    for n in range(12):
        r = await client.post(
            "/auth/login",
            json={"email": f"nokeyfail{n}@example.com", "password": "wrongwrongwrong"},
            headers={"X-Forwarded-For": "not-an-ip"},
        )
        assert r.status_code == 401, r.text

    ok = await client.post(
        "/auth/login",
        json={"email": "nokey@example.com", "password": "hunter2hunter2"},
        headers={"X-Forwarded-For": "not-an-ip"},
    )
    assert ok.status_code == 200, ok.text


@pytest.mark.asyncio
async def test_a_forged_left_most_entry_does_not_dodge_the_throttle(client, make_invite):
    for n in range(5):
        invite = await make_invite()
        r = await client.post(
            "/auth/signup",
            json=_signup_body(invite, email=f"forge{n}@example.com"),
            headers={"X-Forwarded-For": f"10.0.0.{n}, 203.0.113.20"},
        )
        assert r.status_code == 201, r.text

    invite = await make_invite()
    r = await client.post(
        "/auth/signup",
        json=_signup_body(invite, email="forge5@example.com"),
        headers={"X-Forwarded-For": "10.0.0.99, 203.0.113.20"},
    )
    assert r.status_code == 429


# ---------------------------------------------------------------------------
# The email-keyed lockout is untouched (AC 3, AC 9)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_email_lockout_still_fires_at_five_failures(client, make_user):
    await make_user(email="locked@example.com", password="hunter2hunter2")
    # Five failures for one email, spread across addresses so the IP throttle
    # cannot be what rejects the sixth.
    for n in range(5):
        r = await client.post(
            "/auth/login",
            json={"email": "locked@example.com", "password": "wrongwrongwrong"},
            headers={"X-Forwarded-For": f"192.0.2.{n}"},
        )
        assert r.status_code == 401

    locked = await client.post(
        "/auth/login",
        json={"email": "locked@example.com", "password": "hunter2hunter2"},
        headers={"X-Forwarded-For": "192.0.2.100"},
    )
    assert locked.status_code == 401, "the correct password is still refused while locked out"


@pytest.mark.asyncio
async def test_the_email_lockout_still_clears_on_success(client, make_user):
    await make_user(email="forgetful@example.com", password="hunter2hunter2")
    for n in range(4):
        r = await client.post(
            "/auth/login",
            json={"email": "forgetful@example.com", "password": "wrongwrongwrong"},
            headers={"X-Forwarded-For": f"192.0.2.{n}"},
        )
        assert r.status_code == 401

    ok = await client.post(
        "/auth/login",
        json={"email": "forgetful@example.com", "password": "hunter2hunter2"},
        headers={"X-Forwarded-For": "192.0.2.50"},
    )
    assert ok.status_code == 200, ok.text

    # The slate is clean: four more failures for that email do not lock it out.
    for n in range(4):
        r = await client.post(
            "/auth/login",
            json={"email": "forgetful@example.com", "password": "wrongwrongwrong"},
            headers={"X-Forwarded-For": f"192.0.2.{n}"},
        )
        assert r.status_code == 401
    again = await client.post(
        "/auth/login",
        json={"email": "forgetful@example.com", "password": "hunter2hunter2"},
        headers={"X-Forwarded-For": "192.0.2.51"},
    )
    assert again.status_code == 200, again.text
