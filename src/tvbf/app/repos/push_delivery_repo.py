"""Reads and writes on `app.push_delivery`, the delivery log (NEU-1485, project spec §4.3)."""

from datetime import date, datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy import cast as sa_cast
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.types import Date

from tvbf.app.models import (
    PUSH_DELIVERY_FAILED,
    PUSH_DELIVERY_PENDING,
    PUSH_DELIVERY_SENT,
    PushDelivery,
)

# The `error` values the delivery job writes when it retires a subscription
# (§5.2 step 4): the push service answered 404/410, or the fifth consecutive
# failure. `GET /admin/push/stats` counts these as retirements.
RETIREMENT_ERRORS: tuple[str, ...] = ("gone", "failure_limit")

# A `pending` row this old was left by a run that crashed mid-send (§4.3): the
# job counts it as failed and sends again. Younger than this it may be a send
# still in flight, and is left alone.
STALE_PENDING = timedelta(hours=1)


async def insert_pending(
    db: AsyncSession,
    *,
    subscription_id: UUID,
    user_id: UUID,
    notification_key: str,
    kind: str,
    show_id: int | None = None,
    run_id: UUID | None = None,
) -> int | None:
    """Claim one (notification, subscription) pair before sending to it.

    This is the idempotency rule: `ON CONFLICT DO NOTHING` on
    `uq_push_delivery_key_subscription`, so the returned id is the claim and
    `None` means the pair was already claimed — by an earlier run, or by one
    that crashed mid-send — and the caller must not send. The caller commits.
    """
    stmt = (
        pg_insert(PushDelivery)
        .values(
            subscription_id=subscription_id,
            user_id=user_id,
            notification_key=notification_key,
            kind=kind,
            show_id=show_id,
            run_id=run_id,
            status=PUSH_DELIVERY_PENDING,
        )
        .on_conflict_do_nothing(constraint="uq_push_delivery_key_subscription")
        .returning(PushDelivery.id)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def claim_for_run(
    db: AsyncSession,
    *,
    subscription_id: UUID,
    user_id: UUID,
    notification_key: str,
    kind: str,
    show_id: int | None,
    run_id: UUID,
) -> int | None:
    """The delivery job's claim: `insert_pending`, plus the retry rule (§5.2 step 4).

    The unique `(notification_key, subscription_id)` admits one row per pair, so
    a retry cannot be a second row — it re-claims the first. That happens when
    the existing row is `failed`, or is a `pending` older than `STALE_PENDING`
    (a crashed run's, which §4.3 treats as failed). The row is reset to
    `pending` under this run and re-dated, so it records the latest attempt.
    `None` means do not send: the pair was already `sent`, or a fresh `pending`
    may still be in flight. The caller commits.
    """
    stmt = pg_insert(PushDelivery).values(
        subscription_id=subscription_id,
        user_id=user_id,
        notification_key=notification_key,
        kind=kind,
        show_id=show_id,
        run_id=run_id,
        status=PUSH_DELIVERY_PENDING,
    )
    stmt = stmt.on_conflict_do_update(
        constraint="uq_push_delivery_key_subscription",
        set_={
            "status": PUSH_DELIVERY_PENDING,
            "status_code": None,
            "error": None,
            "sent_at": None,
            "run_id": stmt.excluded.run_id,
            "created_at": func.now(),
        },
        where=or_(
            PushDelivery.status == PUSH_DELIVERY_FAILED,
            and_(
                PushDelivery.status == PUSH_DELIVERY_PENDING,
                PushDelivery.created_at < func.now() - STALE_PENDING,
            ),
        ),
    ).returning(PushDelivery.id)
    return (await db.execute(stmt)).scalar_one_or_none()


async def finish(
    db: AsyncSession,
    delivery_id: int,
    *,
    status: str,
    status_code: int | None,
    error: str | None = None,
    sent_at: datetime | None = None,
) -> None:
    """Flip a `pending` row to its terminal status — the one update a delivery
    row ever takes (§7). The caller commits."""
    await db.execute(
        update(PushDelivery)
        .where(PushDelivery.id == delivery_id, PushDelivery.status == PUSH_DELIVERY_PENDING)
        .values(status=status, status_code=status_code, error=error, sent_at=sent_at)
    )


async def count_for_user_since(
    db: AsyncSession, *, user_id: UUID, kind: str, since: datetime
) -> int:
    """How many `kind` rows `user_id` has logged at or after `since`, whatever
    their status and whether or not the subscription still exists."""
    result = await db.execute(
        select(func.count())
        .select_from(PushDelivery)
        .where(
            PushDelivery.user_id == user_id,
            PushDelivery.kind == kind,
            PushDelivery.created_at >= since,
        )
    )
    return result.scalar_one()


async def daily_counts_since(db: AsyncSession, since: date) -> dict[date, tuple[int, int, int]]:
    """`(sent, failed, retired)` per UTC day of `created_at`, for days on or
    after `since` — only days holding a row. A retirement is a `failed` row
    with a `RETIREMENT_ERRORS` error, so it is counted under `failed` as well;
    its `subscription_id` is null by now, which nothing here filters on (§4.3).
    `pending` and `skipped` rows count nowhere."""
    day = sa_cast(func.timezone("UTC", PushDelivery.created_at), Date)
    failed = PushDelivery.status == PUSH_DELIVERY_FAILED
    result = await db.execute(
        select(
            day,
            func.count().filter(PushDelivery.status == PUSH_DELIVERY_SENT),
            func.count().filter(failed),
            func.count().filter(failed, PushDelivery.error.in_(RETIREMENT_ERRORS)),
        )
        .where(day >= since)
        .group_by(day)
    )
    return {d: (sent, failed_n, retired) for d, sent, failed_n, retired in result.all()}


async def purge_before(db: AsyncSession, cutoff: datetime) -> int:
    """Delete rows created before `cutoff` — the retention rule (§5.2 step 5),
    and the one delete this table takes. The caller commits."""
    result = await db.execute(delete(PushDelivery).where(PushDelivery.created_at < cutoff))
    return result.rowcount  # type: ignore[attr-defined]
