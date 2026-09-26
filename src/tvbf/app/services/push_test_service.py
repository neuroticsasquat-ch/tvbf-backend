"""Send one test push to one of the caller's devices (NEU-1486, project spec §5.4).

**Synchronous, in the request.** It is one push, the user asked for it, and they
are watching their phone for it — so the outcome is the response rather than a
202 hiding a failure, and there is no job to hand it to (ADR-0002's carve-out,
spec §7).

The delivery row is **committed as `pending` before the send**, then flipped in
a second transaction. One transaction around the send is the alternative, and it
is worse twice over: it holds a pooled connection for up to the sender's
timeout, and an uncommitted row is invisible to a concurrent request's throttle
count. Committed first, a crash mid-send also leaves the `pending` row §4.3
describes rather than no trace at all. **The delivery job's stale-`pending`
re-send (§4.3) must not pick these rows up**: a test is worth nothing an hour
later. They carry no `run_id` and `kind='test'`, either of which excludes them.

A failure here is logged but **not counted towards retirement**: `failure_count`
is the delivery job's rule for a device that keeps failing (§5.2 step 4), and a
user pressing "send test" against a flaky connection should not be able to
retire their own subscription by doing so. A 404/410 still retires it at once —
that answer means the subscription will never work again, whoever asked.
"""

import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.errors import NotFound, TooManyAttempts
from tvbf.app.models import PUSH_DELIVERY_FAILED, PUSH_DELIVERY_SENT
from tvbf.app.repos import push_delivery_repo, push_subscription_repo
from tvbf.config import Settings
from tvbf.push import sender

KIND = "test"
# The §5.3 `test` payload, less its per-send `key`.
_TITLE = "TV BingeFriend"
_BODY = "Notifications are working"
_URL = "/settings"


@dataclass(frozen=True)
class PushTestOutcome:
    """What happened, as the route reports it. `gone` is its own status because
    it is the one outcome the client must act on — drop its local subscription."""

    status: Literal["sent", "failed", "gone"]
    status_code: int | None


async def send_test_push(
    db: AsyncSession, *, user_id: UUID, subscription_id: UUID, settings: Settings
) -> PushTestOutcome:
    """Throttle, resolve the caller's subscription, log, send, record.

    Raises before anything is written:
        TooManyAttempts — the caller has spent their budget, or already sent a
            test to this device within the same second (the key's grain).
        NotFound — `subscription_id` is not one of the caller's.

    The caller has already checked `settings.vapid_configured`.
    """
    # The ledger is `app.push_delivery` itself, on `report_service`'s precedent:
    # every test is logged anyway, and the row survives the subscription (§4.3),
    # so retiring a device does not refund the budget.
    throttle = settings.push_test_throttle
    since = datetime.now(UTC) - timedelta(minutes=throttle.window_minutes)
    sent = await push_delivery_repo.count_for_user_since(
        db, user_id=user_id, kind=KIND, since=since
    )
    if sent >= throttle.max_attempts:
        raise TooManyAttempts(retry_after_seconds=throttle.window_minutes * 60)

    subscription = await push_subscription_repo.get_for_user(
        db, user_id=user_id, subscription_id=subscription_id
    )
    if subscription is None:
        raise NotFound()

    key = f"{KIND}:{subscription.id}:{int(time.time())}"
    delivery_id = await push_delivery_repo.insert_pending(
        db, subscription_id=subscription.id, user_id=user_id, notification_key=key, kind=KIND
    )
    if delivery_id is None:
        # A double-click: the same device, the same second, so the same key.
        raise TooManyAttempts(retry_after_seconds=1)
    keys = sender.SubscriptionKeys(
        endpoint=subscription.endpoint, p256dh=subscription.p256dh, auth=subscription.auth
    )
    await db.commit()

    payload = {"key": key, "kind": KIND, "title": _TITLE, "body": _BODY, "url": _URL}
    outcome = await sender.send(keys, payload)

    match outcome:
        case sender.Sent(status=code):
            await push_delivery_repo.finish(
                db,
                delivery_id,
                status=PUSH_DELIVERY_SENT,
                status_code=code,
                sent_at=datetime.now(UTC),
            )
            await push_subscription_repo.mark_success(db, subscription_id)
            result = PushTestOutcome(status="sent", status_code=code)
        case sender.Gone(status=code):
            await push_delivery_repo.finish(
                db, delivery_id, status=PUSH_DELIVERY_FAILED, status_code=code, error="gone"
            )
            await push_subscription_repo.delete(db, subscription_id)
            result = PushTestOutcome(status="gone", status_code=code)
        case sender.Failed(status=code, error=error):
            await push_delivery_repo.finish(
                db, delivery_id, status=PUSH_DELIVERY_FAILED, status_code=code, error=error
            )
            result = PushTestOutcome(status="failed", status_code=code)
    await db.commit()
    return result
