"""The daily delivery pass — who hears what today, sent and logged (spec §5.2 steps 4–6).

Selection is `candidates.py`'s and rendering `payloads.py`'s; this module is the
part with side effects: the delivery log, the push service, the retirement rules
and the purge. `jobs/push_deliver.py` is its scheduled entrypoint.

**Each send is two transactions**, the shape `push_test_service` has for the
same reasons: the `pending` claim commits before the push leaves, so a crash
mid-send leaves the row §4.3 describes, and no pooled connection is held across
a push service's timeout. Sequential throughout; a bounded semaphore is the fix
at scale, not a rewrite (§5.2).

**The outcome rule is the job's exit code**: the run finalizes `succeeded`
however many individual sends failed — those are the log's business — and
`failed` only when every send failed, which is a push service outage (or a bad
key) rather than a bad device — so a 404/410 does not count towards it.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from tvbf.app.models import PUSH_DELIVERY_FAILED, PUSH_DELIVERY_SENT, PushSubscription
from tvbf.app.repos import push_delivery_repo, push_subscription_repo
from tvbf.catalog.events import purge_events_before
from tvbf.catalog.runs import finalize_run, record_progress
from tvbf.config import Settings
from tvbf.db import SessionLocal
from tvbf.push import sender
from tvbf.push.candidates import (
    Candidate,
    airs_today_candidates,
    apply_cap,
    event_candidates,
    group_by_user,
)
from tvbf.push.payloads import build_payload

log = logging.getLogger(__name__)

# Consecutive failures before a subscription is retired (Q11).
FAILURE_LIMIT = 5
# How long delivery rows and catalog events are kept (Q17).
RETENTION = timedelta(days=90)


@dataclass
class DeliveryCounts:
    candidates: int = 0
    sent: int = 0
    failed: int = 0
    retired: int = 0
    # The share of `failed` that was a 404/410 — a dead device, not an outage.
    gone: int = 0
    # Already `sent`, or a `pending` too fresh to call crashed.
    skipped: int = 0
    purged: int = 0

    def add(self, other: "DeliveryCounts") -> None:
        self.sent += other.sent
        self.failed += other.failed
        self.retired += other.retired
        self.gone += other.gone
        self.skipped += other.skipped

    @property
    def every_send_failed(self) -> bool:
        """Step 6's outage rule. A 404/410 is the push service answering, so a
        day whose only sends hit dead devices is not an outage."""
        return self.sent == 0 and self.failed > self.gone


async def _deliver_one(
    candidate: Candidate,
    payload: dict[str, str],
    subscription: PushSubscription,
    *,
    run_id: UUID,
    counts: DeliveryCounts,
) -> bool:
    """Claim, send and record one (candidate, subscription). False once the
    subscription is retired — or found deleted by its user mid-run — so the
    caller stops sending to it. One vanished row must not abort the run."""
    async with SessionLocal() as s:
        try:
            delivery_id = await push_delivery_repo.claim_for_run(
                s,
                subscription_id=subscription.id,
                user_id=candidate.user_id,
                notification_key=candidate.key,
                kind=candidate.kind,
                show_id=candidate.show_id,
                run_id=run_id,
            )
            await s.commit()
        except IntegrityError:
            # `fk_push_delivery_subscription`: the subscription was deleted
            # after this user's list was read.
            await s.rollback()
            return False
    if delivery_id is None:
        counts.skipped += 1
        return True

    keys = sender.SubscriptionKeys(
        endpoint=subscription.endpoint, p256dh=subscription.p256dh, auth=subscription.auth
    )
    outcome = await sender.send(keys, payload, ttl=sender.DEFAULT_TTL_SECONDS)

    async with SessionLocal() as s:
        alive = True
        match outcome:
            case sender.Sent(status=code):
                await push_delivery_repo.finish(
                    s,
                    delivery_id,
                    status=PUSH_DELIVERY_SENT,
                    status_code=code,
                    sent_at=datetime.now(UTC),
                )
                await push_subscription_repo.mark_success(s, subscription.id)
                counts.sent += 1
            case sender.Gone(status=code):
                await push_delivery_repo.finish(
                    s, delivery_id, status=PUSH_DELIVERY_FAILED, status_code=code, error="gone"
                )
                await push_subscription_repo.delete(s, subscription.id)
                counts.failed += 1
                counts.gone += 1
                counts.retired += 1
                alive = False
            case sender.Failed(status=code, error=error):
                failures = await push_subscription_repo.record_failure(s, subscription.id)
                if failures is None:
                    alive = False  # deleted by its user mid-send
                elif failures >= FAILURE_LIMIT:
                    error = "failure_limit"
                    alive = False
                    await push_subscription_repo.delete(s, subscription.id)
                    counts.retired += 1
                await push_delivery_repo.finish(
                    s, delivery_id, status=PUSH_DELIVERY_FAILED, status_code=code, error=error
                )
                counts.failed += 1
        await s.commit()
    return alive


async def _deliver_to_user(
    user_id: UUID, candidates: list[Candidate], *, run_id: UUID
) -> DeliveryCounts:
    """Every candidate to every one of the user's subscriptions, in cap order."""
    counts = DeliveryCounts()
    async with SessionLocal() as s:
        subscriptions = await push_subscription_repo.list_for_user(s, user_id)
    for candidate in candidates:
        payload = build_payload(candidate)
        for subscription in list(subscriptions):
            if not await _deliver_one(
                candidate, payload, subscription, run_id=run_id, counts=counts
            ):
                subscriptions.remove(subscription)
    return counts


async def run_push_delivery(run_id: UUID, settings: Settings, *, now: datetime) -> DeliveryCounts:
    """Steps 1–5 of §5.2: gather, cap, deliver, purge. Finalizing is the caller's.

    `today` is `now`'s UTC date — at the 13:00 UTC schedule, the US-Eastern date,
    the same clock `/me/upcoming` reads.
    """
    today = now.astimezone(UTC).date()
    async with SessionLocal() as s:
        airs = await airs_today_candidates(s, today=today)
        events = await event_candidates(s, now=now, window_hours=settings.push_event_window_hours)
    capped = apply_cap(group_by_user(airs + events), settings.push_daily_cap, today=today)

    counts = DeliveryCounts(candidates=sum(len(c) for c in capped.values()))
    for user_id, candidates in capped.items():
        user_counts = await _deliver_to_user(user_id, candidates, run_id=run_id)
        counts.add(user_counts)
        log.info(
            "push delivery user=%s candidates=%d sent=%d failed=%d retired=%d skipped=%d",
            user_id,
            len(candidates),
            user_counts.sent,
            user_counts.failed,
            user_counts.retired,
            user_counts.skipped,
        )
        async with SessionLocal() as s:
            await record_progress(
                s, run_id, processed_delta=user_counts.sent, failed_delta=user_counts.failed
            )
            await s.commit()

    cutoff = now - RETENTION
    async with SessionLocal() as s:
        counts.purged = await purge_events_before(s, cutoff)
        counts.purged += await push_delivery_repo.purge_before(s, cutoff)
        await s.commit()
    return counts


async def run_push_delivery_job(run_id: UUID, settings: Settings) -> None:
    """One delivery run, guaranteed to finalize — the worker `run_scheduled_delta`
    awaits. `failed` only when every send failed (step 6), or on a crash."""
    try:
        counts = await run_push_delivery(run_id, settings, now=datetime.now(UTC))
    except Exception as e:
        log.exception("push delivery crashed")
        async with SessionLocal() as s:
            await finalize_run(s, run_id, status="failed", error=str(e))
            await s.commit()
        return

    summary = (
        f"candidates={counts.candidates} sent={counts.sent} failed={counts.failed} "
        f"retired={counts.retired} skipped={counts.skipped} purged={counts.purged}"
    )
    log.info("push delivery run %s: %s", run_id, summary)
    async with SessionLocal() as s:
        if counts.every_send_failed:
            await finalize_run(s, run_id, status="failed", error=f"every send failed: {summary}")
        else:
            await finalize_run(s, run_id, status="succeeded")
        await s.commit()
