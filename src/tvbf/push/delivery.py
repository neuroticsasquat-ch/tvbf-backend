"""The daily delivery passes — who hears what today, sent and logged (spec §5.2 steps 4–6).

Selection is `candidates.py`'s and rendering `payloads.py`'s; this module is the
part with side effects: the delivery log, the push service, the retirement rules
and the purge. **Two delivery tasks share it** (NEU-1540): `AIRS_TODAY`, which
reads the schedule, and `EVENTS`, which reads catalog events and owns the purge.
Each is one `DeliveryTask` and one scheduled entrypoint —
`jobs/push_airs_today.py` and `jobs/push_events.py` — with its own run kind, its
own deadman and its own optional cap; neither waits on or caps the other.

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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.models import PUSH_DELIVERY_FAILED, PUSH_DELIVERY_SENT, PushSubscription
from tvbf.app.repos import push_delivery_repo, push_subscription_repo
from tvbf.catalog.events import purge_events_before
from tvbf.catalog.runs import finalize_run, record_progress
from tvbf.config import Settings
from tvbf.db import SessionLocal
from tvbf.jobs.scheduled import ping, run_scheduled_delta
from tvbf.push import sender
from tvbf.push.candidates import (
    Candidate,
    DeliveryTaskLabel,
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


@dataclass(frozen=True)
class DeliveryTask:
    """What differs between the two delivery tasks; everything else is shared.

    `gather` is the task's half of §5.2 steps 1–2. `cap` and `healthcheck_url`
    read `Settings` rather than holding a value because settings are loaded at
    run time. `label` keys and titles the task's summary.
    """

    kind: str
    name: str
    label: DeliveryTaskLabel
    gather: Callable[[AsyncSession, datetime, Settings], Awaitable[list[Candidate]]]
    cap: Callable[[Settings], int]
    healthcheck_url: Callable[[Settings], str | None]
    purges: bool


async def _gather_airs_today(
    session: AsyncSession, now: datetime, settings: Settings
) -> list[Candidate]:
    return await airs_today_candidates(session, today=now.astimezone(UTC).date())


async def _gather_events(
    session: AsyncSession, now: datetime, settings: Settings
) -> list[Candidate]:
    return await event_candidates(session, now=now, window_hours=settings.push_event_window_hours)


AIRS_TODAY = DeliveryTask(
    kind="push_airs_today",
    name="push airs-today",
    label="airs_today",
    gather=_gather_airs_today,
    cap=lambda s: s.push_airs_today_daily_cap,
    healthcheck_url=lambda s: s.healthcheck_push_airs_today_url,
    purges=False,
)
# The events task owns the 90-day purge of both tables (NEU-1540), so it runs
# once a day from one task; the airs-today task reads and sends, nothing else.
EVENTS = DeliveryTask(
    kind="push_events",
    name="push events",
    label="events",
    gather=_gather_events,
    cap=lambda s: s.push_events_daily_cap,
    healthcheck_url=lambda s: s.healthcheck_push_events_url,
    purges=True,
)


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


async def run_push_delivery(
    task: DeliveryTask, run_id: UUID, settings: Settings, *, now: datetime
) -> DeliveryCounts:
    """Steps 1–5 of §5.2 for one task: gather, cap, deliver, and — for the
    events task only — purge. Finalizing is the caller's.

    `today` is `now`'s UTC date — at both the 13:00 and the 17:00 UTC schedule,
    the US-Eastern date, the same clock `/me/upcoming` reads.
    """
    today = now.astimezone(UTC).date()
    async with SessionLocal() as s:
        gathered = await task.gather(s, now, settings)
    capped = apply_cap(group_by_user(gathered), task.cap(settings), task=task.label, today=today)

    counts = DeliveryCounts(candidates=sum(len(c) for c in capped.values()))
    for user_id, candidates in capped.items():
        user_counts = await _deliver_to_user(user_id, candidates, run_id=run_id)
        counts.add(user_counts)
        log.info(
            "%s user=%s candidates=%d sent=%d failed=%d retired=%d skipped=%d",
            task.name,
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

    if task.purges:
        cutoff = now - RETENTION
        async with SessionLocal() as s:
            counts.purged = await purge_events_before(s, cutoff)
            counts.purged += await push_delivery_repo.purge_before(s, cutoff)
            await s.commit()
    return counts


async def run_push_delivery_job(task: DeliveryTask, run_id: UUID, settings: Settings) -> None:
    """One delivery run, guaranteed to finalize — the worker `run_scheduled_delta`
    awaits. `failed` only when every send failed (step 6), or on a crash."""
    try:
        counts = await run_push_delivery(task, run_id, settings, now=datetime.now(UTC))
    except Exception as e:
        log.exception("%s crashed", task.name)
        async with SessionLocal() as s:
            await finalize_run(s, run_id, status="failed", error=str(e))
            await s.commit()
        return

    summary = (
        f"candidates={counts.candidates} sent={counts.sent} failed={counts.failed} "
        f"retired={counts.retired} skipped={counts.skipped} purged={counts.purged}"
    )
    log.info("%s run %s: %s", task.name, run_id, summary)
    async with SessionLocal() as s:
        if counts.every_send_failed:
            await finalize_run(s, run_id, status="failed", error=f"every send failed: {summary}")
        else:
            await finalize_run(s, run_id, status="succeeded")
        await s.commit()


async def run_delivery_task(task: DeliveryTask, settings: Settings) -> bool:
    """One scheduled run of `task`, on `jobs/scheduled.py`'s shape. True iff it
    finished `succeeded` — what both entrypoints return from.

    **Refuses to start without VAPID keys**: logged, `/fail` pinged, no run row.
    A task that cannot sign a single push would otherwise log every candidate
    as a failure — or, if nobody is subscribed yet, succeed silently for as long
    as the keys stay missing.
    """
    healthcheck_url = task.healthcheck_url(settings)
    if not settings.vapid_configured:
        log.error(
            "%s refused: VAPID_PRIVATE_KEY, VAPID_PUBLIC_KEY and VAPID_SUBJECT must all be set",
            task.name,
        )
        await ping(healthcheck_url, "/fail")
        return False

    async def worker(run_id: UUID, settings: Settings) -> None:
        await run_push_delivery_job(task, run_id, settings)

    return await run_scheduled_delta(
        settings=settings,
        kind=task.kind,
        worker=worker,
        healthcheck_url=healthcheck_url,
        name=task.name,
    )
