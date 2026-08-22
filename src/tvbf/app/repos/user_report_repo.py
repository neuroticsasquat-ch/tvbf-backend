"""Reports one user files about another, and the ledger the report throttle
counts (NEU-1162). Repo: pure DB I/O, no business logic, no commits.

The throttle has **no side table**, unlike `auth_attempt`: every report is
persisted anyway, so counting the reports themselves removes the "record the
attempt" step there is no way to get wrong. `ix_user_report_reporter_created`
is exactly `count_since`'s query.

It also owns the admin queue's read (NEU-1197). That is one module rather than
two because splitting would put two queries against one five-column table in
separate files; `admin_users.py` gets away with an inline `select(User)` only
because it is a bare unfiltered scan, where this is a two-sided join with a
filter and a count.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from tvbf.app.models import User, UserReport


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


async def list_for_admin(
    db: AsyncSession,
    *,
    reported_user_id: UUID | None,
    page: int,
    per_page: int,
) -> tuple[list[tuple[UserReport, User, User]], int]:
    """One page of the admin queue plus its total, as `(report, reporter,
    reported_user)` triples (NEU-1197 §8).

    Both `app.user` joins use `aliased(User)` so one round trip hydrates both
    parties. Two queries per request — page and count — the same budget as
    `GET /shows`.

    **The queue filters nothing.** NEU-1162 §4 put four predicates on
    `disabled_at IS NULL`; none of them belongs here. Those are about what a
    *stranger* may see, and this is the one read path whose purpose is to see
    what strangers cannot: a report *about* a disabled account is how you tell
    it has already been dealt with, and a report *by* one must not be
    retroactively erased by the disabling it justified.

    Both FKs are `NOT NULL` with `ON DELETE CASCADE`, so an `INNER JOIN` can
    neither drop a row nor fan one out (§4.1) — which is also why the count runs
    against `user_report` alone rather than the joined statement.

    `ORDER BY created_at DESC, id DESC`: `created_at` defaults to
    transaction-start time, so concurrent reports can carry byte-identical
    timestamps, and without the tiebreak such a pair can appear on both page 1
    and page 2, or on neither.
    """
    reporter = aliased(User)
    reported = aliased(User)

    stmt = (
        select(UserReport, reporter, reported)
        .join(reporter, UserReport.reporter_id == reporter.id)
        .join(reported, UserReport.reported_user_id == reported.id)
    )
    count_stmt = select(func.count()).select_from(UserReport)
    if reported_user_id is not None:
        stmt = stmt.where(UserReport.reported_user_id == reported_user_id)
        count_stmt = count_stmt.where(UserReport.reported_user_id == reported_user_id)

    stmt = (
        stmt.order_by(UserReport.created_at.desc(), UserReport.id.desc())
        .limit(per_page)
        .offset((page - 1) * per_page)
    )

    rows = list((await db.execute(stmt)).tuples().all())
    total = (await db.execute(count_stmt)).scalar_one()
    return rows, total
