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
from tvbf.db import Base  # noqa: E402
from tvbf.tvmaze import models as _tvmaze_models  # noqa: F401, E402 -- register tables


@pytest.fixture(scope="session")
async def test_engine():
    url = os.environ["TEST_DATABASE_URL"]
    engine = create_async_engine(url, pool_pre_ping=True)
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA IF EXISTS tvmaze CASCADE"))
        await conn.execute(text("DROP SCHEMA IF EXISTS app CASCADE"))
        await conn.execute(text("CREATE SCHEMA tvmaze"))
        await conn.execute(text("CREATE SCHEMA app"))
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS citext"))
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA IF EXISTS tvmaze CASCADE"))
        await conn.execute(text("DROP SCHEMA IF EXISTS app CASCADE"))
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
    )
    from tvbf.routers import admin_invites

    captured: list[dict[str, str]] = []

    async def _fake(*, to: str, subject: str, html: str, text: str) -> None:
        captured.append({"to": to, "subject": subject, "html": html, "text": text})

    modules = (
        email_verification_service,
        email_change_service,
        password_reset_service,
        feedback_service,
        admin_invites,
    )
    originals = [m.send_email for m in modules]
    for m in modules:
        m.send_email = _fake  # type: ignore[assignment]
    try:
        yield captured
    finally:
        for m, original in zip(modules, originals, strict=True):
            m.send_email = original  # type: ignore[assignment]


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
                "WHERE schemaname IN ('tvmaze', 'app')"
            )
        )
        tables = [r[0] for r in result]
        if tables:
            await conn.execute(text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE"))
