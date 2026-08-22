import os

os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
# Tests use ASGITransport with a synthetic base_url ("https://test"), which
# means a parent-domain cookie like ".tvbf.localhost" is silently dropped by
# httpx's cookie jar as not applicable. Force host-only cookies during the
# test run regardless of what the dev container exports.
os.environ.pop("COOKIE_DOMAIN", None)

pytest_plugins = ["tests.fixtures.users"]

from collections.abc import AsyncIterator  # noqa: E402

import pytest  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from tvbf.app import models as _app_models  # noqa: F401, E402 -- register tables
from tvbf.catalog import models as _catalog_models  # noqa: F401, E402 -- register tables
from tvbf.db import Base  # noqa: E402
from tvbf.rate_budget import reset_rate_limiters  # noqa: E402


@pytest.fixture(scope="session")
async def test_engine():
    url = os.environ["TEST_DATABASE_URL"]
    engine = create_async_engine(url, pool_pre_ping=True)
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA IF EXISTS app CASCADE"))
        await conn.execute(text("DROP SCHEMA IF EXISTS catalog CASCADE"))
        await conn.execute(text("CREATE SCHEMA app"))
        await conn.execute(text("CREATE SCHEMA catalog"))
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS citext"))
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS unaccent"))
        await conn.execute(
            text("""
            CREATE OR REPLACE FUNCTION immutable_unaccent(text)
            RETURNS text
            LANGUAGE sql
            IMMUTABLE STRICT
            AS $$ SELECT public.unaccent($1) $$
        """)
        )
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA IF EXISTS app CASCADE"))
        await conn.execute(text("DROP SCHEMA IF EXISTS catalog CASCADE"))
    await engine.dispose()


@pytest.fixture(autouse=True)
def _stub_outbound_email():
    """Replace the email sender with an in-memory capture so test runs never
    hit Mailpit. Tests that need to assert can name this fixture explicitly.

    Deliberately avoids the built-in `monkeypatch` fixture — having an autouse
    fixture request `monkeypatch` would force `monkeypatch`'s teardown to run
    after the `session` fixture's, which breaks admin tests that patch
    `asyncio.create_task` (SQLAlchemy's AsyncSession close calls it).
    """
    from tvbf.app.services import (
        email_change_service,
        email_verification_service,
        feedback_service,
        password_reset_service,
        report_service,
    )
    from tvbf.routers import admin_invites
    from tvbf.routers import contact as contact_router

    captured: list[dict[str, str | None]] = []

    async def _fake(
        *, to: str, subject: str, html: str, text: str, reply_to: str | None = None
    ) -> None:
        captured.append(
            {"to": to, "subject": subject, "html": html, "text": text, "reply_to": reply_to}
        )

    modules = (
        email_verification_service,
        email_change_service,
        password_reset_service,
        feedback_service,
        report_service,
        admin_invites,
        contact_router,
    )
    originals = [m.send_email for m in modules]
    for m in modules:
        m.send_email = _fake  # type: ignore[assignment]
    try:
        yield captured
    finally:
        for m, original in zip(modules, originals, strict=True):
            m.send_email = original  # type: ignore[assignment]


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """Hand every test a fresh, in-process rate limiter for every source.

    Two things happen here.

    `get_rate_limiter` is `@cache`d so all clients in a process share one
    budget per source (NEU-955). Left alone, its bucket would carry across tests
    and make a later test wait off an earlier test's requests, and the
    second-budget warning (NEU-957) would fire on whichever test happened to ask
    for a different budget second.

    It also builds a `DatabaseRateLimiter` in production, because the budget
    now spans processes (ADR-0006). Swapping `build_limiter` for one returning
    the in-process `RateLimiter` keeps `tests/unit` free of a database — ~50
    client constructions across the suite do not pass `limiter=`, and making
    each one a DB round-trip per request would buy nothing: the shared buckets
    have their own integration tests in `tests/integration/test_rate_budget.py`.

    Like `_stub_outbound_email`, this deliberately does not request
    `monkeypatch` — an autouse fixture that does would run its teardown after
    the `session` fixture's and break the admin tests' `asyncio.create_task`
    patching.
    """
    from tvbf import rate_budget as rate_budget_module

    original = rate_budget_module.build_limiter

    def _in_process(bucket, budget):
        return rate_budget_module.RateLimiter(budget.calls, budget.window_seconds)

    rate_budget_module.build_limiter = _in_process  # type: ignore[assignment]
    reset_rate_limiters()
    try:
        yield
    finally:
        reset_rate_limiters()
        rate_budget_module.build_limiter = original  # type: ignore[assignment]


@pytest.fixture
async def session(test_engine) -> AsyncIterator[AsyncSession]:
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker() as s:
        yield s
        await s.rollback()
    async with test_engine.begin() as conn:
        result = await conn.execute(
            text(
                "SELECT schemaname || '.' || tablename FROM pg_tables "
                "WHERE schemaname IN ('app', 'catalog')"
            )
        )
        tables = [r[0] for r in result]
        if tables:
            await conn.execute(text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE"))
