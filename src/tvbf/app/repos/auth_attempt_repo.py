"""Signup and failed-login attempts keyed on the client address, so
`auth_throttle` can throttle credential stuffing that walks a different email
on every request. Repo: pure DB I/O, no business logic, no commits."""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.models import AuthAttempt

# The checked vocabulary of `ck_auth_attempt_kind`. A third kind widens the
# constraint too, deliberately loud.
SIGNUP = "signup"
LOGIN = "login"


async def record(db: AsyncSession, *, kind: str, ip: str) -> None:
    """Insert an attempt row. Caller commits."""
    db.add(AuthAttempt(kind=kind, ip=ip))
    await db.flush()


async def count_since(db: AsyncSession, *, kind: str, ip: str, since: datetime) -> int:
    """Number of `kind` attempts from `ip` recorded at or after `since`."""
    result = await db.execute(
        select(func.count())
        .select_from(AuthAttempt)
        .where(
            AuthAttempt.kind == kind,
            AuthAttempt.ip == ip,
            AuthAttempt.attempted_at >= since,
        )
    )
    return result.scalar_one()
