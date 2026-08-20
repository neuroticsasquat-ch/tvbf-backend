"""The connection-request ledger `connection_throttle` counts (NEU-1157).
Repo: pure DB I/O, no business logic, no commits.

`ix_connection_request_log_requester_created` is exactly the shape of the two
*counting* queries here — one requester's rows since a timestamp.
`has_decline_since` is the third query and is deliberately not that shape: it
filters the ordered pair on `resolved_at`, which the index does not carry. It
still leads on `requester_id`, and it is a `LIMIT 1` over one requester's rows,
so a second index would be bought for nothing.

The five outcomes are the checked vocabulary of
`ck_connection_request_log_outcome`. A sixth widens the constraint too,
deliberately loud, following `auth_attempt_repo`.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import case, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.models import ConnectionRequestLog

PENDING = "pending"
ACCEPTED = "accepted"
DECLINED = "declined"
CANCELLED = "cancelled"
BLOCKED = "blocked"

#: Adverse outcomes that are already resolved. "Ignored" — still `pending` past
#: the aging threshold — is the third, and it is a predicate on `created_at`
#: rather than a stored value, so it cannot live in this tuple.
_RESOLVED_ADVERSE = (DECLINED, BLOCKED)


async def record(
    db: AsyncSession, *, requester_id: UUID, addressee_id: UUID
) -> ConnectionRequestLog:
    """Insert a `pending` ledger row and flush so `id` and `created_at` are
    populated. Caller commits."""
    row = ConnectionRequestLog(
        requester_id=requester_id,
        addressee_id=addressee_id,
        outcome=PENDING,
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return row


async def resolve(
    db: AsyncSession,
    *,
    requester_id: UUID,
    addressee_id: UUID,
    outcome: str,
    resolved_at: datetime,
) -> None:
    """Move this pair's `pending` row to a terminal outcome. Caller commits.

    **A missing row is normal and this no-ops**, never raises. Three ordinary
    situations reach here and find nothing: a request created before the
    migration, a row already cascaded away by an account deletion, and a `block`
    where no request ever existed — the common case for `block`. An `UPDATE`
    matching zero rows is the shape that makes that free; `scalar_one()` here
    would turn an ordinary accept into a 500.

    The pair is **ordered** — `requester_id` is who sent the request, not who is
    acting now — so a decline and a cancel update the same row.

    It is scoped to the pair rather than to one request id, which is safe
    because a pair can hold **at most one `pending` row at a time**: a pending
    `app.connection` row 409s a second request, so the previous one must have
    resolved before another could be created. The `UPDATE` would move all of
    them if that ever stopped being true, which is the honest failure — a
    `LIMIT 1` would silently leave the rest pending and aging toward
    "ignored".
    """
    await db.execute(
        update(ConnectionRequestLog)
        .where(
            ConnectionRequestLog.requester_id == requester_id,
            ConnectionRequestLog.addressee_id == addressee_id,
            ConnectionRequestLog.outcome == PENDING,
        )
        .values(outcome=outcome, resolved_at=resolved_at)
    )


async def count_created_since(db: AsyncSession, *, requester_id: UUID, since: datetime) -> int:
    """Number of requests `requester_id` created at or after `since`.

    **All five outcomes count.** The count is of rows *created*, so cancelling a
    request does not return its slot and cancel-and-re-send is worthless as a
    reset (§3.1, AC 3).
    """
    result = await db.execute(
        select(func.count())
        .select_from(ConnectionRequestLog)
        .where(
            ConnectionRequestLog.requester_id == requester_id,
            ConnectionRequestLog.created_at >= since,
        )
    )
    return result.scalar_one()


async def reputation_counts(
    db: AsyncSession, *, requester_id: UUID, since: datetime, ignored_before: datetime
) -> tuple[int, int]:
    """`(accepted, adverse)` over `requester_id`'s rows created at or after
    `since` (§3.2).

    Adverse is `declined` + `blocked` + **ignored**: still `pending` and created
    before `ignored_before`. `cancelled` and young `pending` rows are in
    neither, so they move neither numerator nor denominator — see the service
    for why each exclusion is deliberate.

    Both windows are anchored on `created_at`, never `resolved_at`: a row's
    membership is then stable and the rate is a statement about the requester's
    *sending* behaviour, which is what is being governed.
    """
    is_ignored = (ConnectionRequestLog.outcome == PENDING) & (
        ConnectionRequestLog.created_at < ignored_before
    )
    # `count()` over a `CASE` with no `ELSE` counts the non-null arms, which is
    # a conditional count in one pass rather than two queries.
    result = await db.execute(
        select(
            func.count(case((ConnectionRequestLog.outcome == ACCEPTED, 1))),
            func.count(case((ConnectionRequestLog.outcome.in_(_RESOLVED_ADVERSE) | is_ignored, 1))),
        ).where(
            ConnectionRequestLog.requester_id == requester_id,
            ConnectionRequestLog.created_at >= since,
        )
    )
    accepted_count, adverse_count = result.one()
    return accepted_count, adverse_count


async def has_decline_since(
    db: AsyncSession, *, requester_id: UUID, addressee_id: UUID, since: datetime
) -> bool:
    """True when `addressee_id` declined `requester_id` at or after `since`.

    Anchored on `resolved_at` — the cooldown runs from the moment of the
    decline, which is the event the accepted criterion counts days from — and
    scoped to `declined` only. Not `cancelled`: your own withdrawal must not
    lock you out of correcting it. Not `blocked`: the `blocked` row persists in
    `app.connection`, so `find_pair` already 409s that pair while it stands.
    """
    result = await db.execute(
        select(ConnectionRequestLog.id)
        .where(
            ConnectionRequestLog.requester_id == requester_id,
            ConnectionRequestLog.addressee_id == addressee_id,
            ConnectionRequestLog.outcome == DECLINED,
            ConnectionRequestLog.resolved_at >= since,
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is not None
