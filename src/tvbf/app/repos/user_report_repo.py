"""Reports one user files about another, and the ledger the report throttle
counts (NEU-1162). Repo: pure DB I/O, no business logic, no commits.

The throttle has **no side table**, unlike `auth_attempt`: every report is
persisted anyway, so counting the reports themselves removes the "record the
attempt" step there is no way to get wrong. `ix_user_report_reporter_created`
is exactly `count_since`'s query.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.models import UserReport


async def record(
    db: AsyncSession, *, reporter_id: UUID, reported_user_id: UUID, reason: str
) -> UserReport:
    """Insert a report row and flush so `id` and `created_at` are populated.
    Caller commits."""
    row = UserReport(
        reporter_id=reporter_id,
        reported_user_id=reported_user_id,
        reason=reason,
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return row


async def count_since(db: AsyncSession, *, reporter_id: UUID, since: datetime) -> int:
    """Number of reports `reporter_id` filed at or after `since`."""
    result = await db.execute(
        select(func.count())
        .select_from(UserReport)
        .where(
            UserReport.reporter_id == reporter_id,
            UserReport.created_at >= since,
        )
    )
    return result.scalar_one()
