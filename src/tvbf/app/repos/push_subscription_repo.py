"""Reads and writes on `app.push_subscription` (NEU-1485, project spec §4.2)."""

from uuid import UUID

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.models import PushSubscription


async def upsert(
    db: AsyncSession,
    *,
    user_id: UUID,
    endpoint: str,
    p256dh: str,
    auth: str,
    user_agent: str | None,
) -> UUID:
    """Insert the subscription, or take over an existing `endpoint` for `user_id`.

    An endpoint belongs to exactly one user, and whoever subscribes with it now
    is that user (§4.2). **A row another user holds is deleted, not re-owned**:
    its id carries that user's delivery log, and `uq_push_delivery_key_subscription`
    would otherwise skip the new owner for any notification the old one already
    received. The delete nulls those rows' `subscription_id` and they keep their
    own `user_id`, so nothing already logged is re-attributed, and the new row
    starts with its own id, `created_at` and history.

    For the caller's own endpoint the row is updated in place, keeping its id:
    keys and user agent are replaced, and `failure_count` resets, because a
    re-issued subscription carries new keys and failures against the old ones
    say nothing about these. The caller commits.
    """
    await db.execute(
        sa_delete(PushSubscription).where(
            PushSubscription.endpoint == endpoint, PushSubscription.user_id != user_id
        )
    )
    stmt = pg_insert(PushSubscription).values(
        user_id=user_id,
        endpoint=endpoint,
        p256dh=p256dh,
        auth=auth,
        user_agent=user_agent,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[PushSubscription.endpoint],
        set_={
            "p256dh": stmt.excluded.p256dh,
            "auth": stmt.excluded.auth,
            "user_agent": stmt.excluded.user_agent,
            "failure_count": 0,
        },
    ).returning(PushSubscription.id)
    return (await db.execute(stmt)).scalar_one()


async def list_for_user(db: AsyncSession, user_id: UUID) -> list[PushSubscription]:
    """The user's subscriptions, newest first."""
    result = await db.execute(
        select(PushSubscription)
        .where(PushSubscription.user_id == user_id)
        .order_by(PushSubscription.created_at.desc(), PushSubscription.id)
    )
    return list(result.scalars().all())


async def count_totals(db: AsyncSession) -> tuple[int, int]:
    """`(subscriptions, distinct users holding one)` across every account."""
    result = await db.execute(select(func.count(), func.count(PushSubscription.user_id.distinct())))
    subscriptions, users = result.one()
    return subscriptions, users


async def get_for_user(
    db: AsyncSession, *, user_id: UUID, subscription_id: UUID
) -> PushSubscription | None:
    """The subscription iff it is `user_id`'s — `None` for another user's id
    and for an unknown one alike."""
    result = await db.execute(
        select(PushSubscription).where(
            PushSubscription.id == subscription_id, PushSubscription.user_id == user_id
        )
    )
    return result.scalar_one_or_none()


async def mark_success(db: AsyncSession, subscription_id: UUID) -> None:
    """Record a 2xx: stamp `last_success_at` and reset the consecutive-failure
    count (§5.2 step 4). The caller commits."""
    await db.execute(
        update(PushSubscription)
        .where(PushSubscription.id == subscription_id)
        .values(last_success_at=func.now(), failure_count=0)
    )


async def record_failure(db: AsyncSession, subscription_id: UUID) -> int | None:
    """Count one more consecutive failure and return the new count, which the
    delivery job retires the subscription on (§5.2 step 4) — or `None` when the
    row is gone, because its user deleted it mid-send. The caller commits."""
    result = await db.execute(
        update(PushSubscription)
        .where(PushSubscription.id == subscription_id)
        .values(failure_count=PushSubscription.failure_count + 1)
        .returning(PushSubscription.failure_count)
    )
    return result.scalar_one_or_none()


async def delete(db: AsyncSession, subscription_id: UUID) -> None:
    """Retire a subscription: the push service answered 404/410 for it, or it
    reached the delivery job's failure limit. Its delivery rows survive with
    `subscription_id` nulled (§4.3). The caller commits."""
    await db.execute(sa_delete(PushSubscription).where(PushSubscription.id == subscription_id))


async def delete_for_user(db: AsyncSession, *, user_id: UUID, subscription_id: UUID) -> int:
    """Delete one subscription iff it is `user_id`'s. Returns the rowcount, so
    the caller can tell "not yours" (0) from done (1). The caller commits."""
    result = await db.execute(
        sa_delete(PushSubscription).where(
            PushSubscription.id == subscription_id, PushSubscription.user_id == user_id
        )
    )
    return result.rowcount  # type: ignore[attr-defined]


async def delete_all_for_user(db: AsyncSession, user_id: UUID) -> int:
    """Delete every subscription `user_id` has. The caller commits."""
    result = await db.execute(
        sa_delete(PushSubscription).where(PushSubscription.user_id == user_id)
    )
    return result.rowcount  # type: ignore[attr-defined]
