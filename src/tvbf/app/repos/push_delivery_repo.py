"""Writes to `app.push_delivery`, the delivery log (NEU-1485, project spec §4.3)."""

from uuid import UUID

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.models import PUSH_DELIVERY_PENDING, PushDelivery


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
