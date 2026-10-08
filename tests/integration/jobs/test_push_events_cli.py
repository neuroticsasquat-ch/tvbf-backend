"""The daily catalog-event push, end to end against the test database (NEU-1540, spec §1).

The delivery rules themselves — claim, retry, retirement, the exit code — are
the airs-today task's too and are exercised in full in
`test_push_airs_today_cli.py`; here each is checked once against an event, with
what is the events task's alone: no airs-today pushes, its own cap and summary
key, and the 90-day purge.
"""

from datetime import UTC, datetime, timedelta

import httpx
import respx
from sqlalchemy import select

from tests.fixtures.push_delivery import (
    SHOW,
    airs_today_key,
    deliveries,
    push_settings,
    seed_airs_today,
    seed_ended_event,
    stub_send,
    subscriber,
    subscriptions,
    the_run,
    today,
)
from tvbf.app.models import PushDelivery
from tvbf.catalog import models as m
from tvbf.catalog.runs import create_run
from tvbf.jobs import push_events
from tvbf.push import sender

HEALTHCHECK = "https://hc.example.com/push-events"
ENDED = SHOW + 1


async def _run_task(**overrides) -> bool:
    return await push_events.run_push_events(push_settings(**overrides))


# --- delivery ---------------------------------------------------------------


async def test_an_event_is_sent_to_every_device_and_logged(session, make_user, monkeypatch):
    key = await seed_ended_event(session, ENDED)
    user_id = (await subscriber(make_user, session, "phone", "laptop", shows=(ENDED,))).id
    calls = stub_send(monkeypatch)

    assert await _run_task() is True

    assert [payload["key"] for _, payload in calls] == [key, key]
    assert calls[0][1]["body"] == "The series has ended"
    run = await the_run(session)
    assert (run.kind, run.status, run.shows_processed) == ("push_events", "succeeded", 2)
    rows = await deliveries(session)
    assert [(r.status, r.kind, r.show_id, r.user_id) for r in rows] == [
        ("sent", "ended", ENDED, user_id)
    ] * 2


async def test_airs_today_is_not_this_tasks_to_send(session, make_user, monkeypatch):
    await seed_airs_today(session)
    key = await seed_ended_event(session, ENDED)
    await subscriber(make_user, session, shows=(SHOW, ENDED))
    calls = stub_send(monkeypatch)

    assert await _run_task() is True

    assert [payload["key"] for _, payload in calls] == [key]


async def test_there_is_no_cap_by_default(session, make_user, monkeypatch):
    shows = tuple(range(ENDED, ENDED + 7))
    for show_id in shows:
        await seed_ended_event(session, show_id, name=f"Show {show_id}")
    await subscriber(make_user, session, shows=shows)
    calls = stub_send(monkeypatch)

    assert await _run_task() is True

    assert len(calls) == 7
    assert all(payload["kind"] == "ended" for _, payload in calls)


async def test_past_its_cap_the_rest_folds_into_its_own_summary(session, make_user, monkeypatch):
    """AC 3's events half: a cap of 1 and two events — on a day the airs-today
    task already sent its own summary, which this one must not collide with."""
    first = await seed_ended_event(session, ENDED, name="Andor")
    await seed_ended_event(session, ENDED + 1, name="Severance")
    user_id = (await subscriber(make_user, session, shows=(ENDED, ENDED + 1))).id
    [subscription] = await subscriptions(session)
    airs_summary = f"summary:airs_today:{user_id}:{today().isoformat()}"
    session.add(
        PushDelivery(
            subscription_id=subscription.id,
            user_id=user_id,
            notification_key=airs_summary,
            kind="summary",
            status="sent",
        )
    )
    await session.commit()
    calls = stub_send(monkeypatch)

    assert await _run_task(push_events_daily_cap=1) is True

    events_summary = f"summary:events:{user_id}:{today().isoformat()}"
    assert [payload["key"] for _, payload in calls] == [first, events_summary]
    assert calls[-1][1]["title"] == "1 more update today"
    assert calls[-1][1]["body"] == "Severance"
    statuses = {r.notification_key: r.status for r in await deliveries(session)}
    assert statuses[airs_summary] == statuses[events_summary] == "sent"


async def test_a_second_run_sends_nothing_again(session, make_user, monkeypatch):
    await seed_ended_event(session, ENDED)
    await subscriber(make_user, session, shows=(ENDED,))
    calls = stub_send(monkeypatch)

    assert await _run_task() is True
    assert await _run_task() is True

    assert len(calls) == 1
    assert [r.status for r in await deliveries(session)] == ["sent"]


async def test_a_failed_send_is_retried_on_the_next_run(session, make_user, monkeypatch):
    await seed_ended_event(session, ENDED)
    await subscriber(make_user, session, shows=(ENDED,))
    stub_send(monkeypatch, sender.Failed(status=500, error="boom"))
    await _run_task()
    calls = stub_send(monkeypatch)

    assert await _run_task() is True

    assert len(calls) == 1
    [row] = await deliveries(session)
    assert (row.status, row.error) == ("sent", None)


async def test_a_gone_subscription_is_retired(session, make_user, monkeypatch):
    await seed_ended_event(session, ENDED)
    await subscriber(make_user, session, shows=(ENDED,))
    stub_send(monkeypatch, sender.Gone(status=410))

    # A day whose only sends hit dead devices is not an outage.
    assert await _run_task() is True

    assert await subscriptions(session) == []
    [row] = await deliveries(session)
    assert (row.status, row.error, row.subscription_id) == ("failed", "gone", None)


# --- purge ------------------------------------------------------------------


async def test_rows_older_than_ninety_days_are_purged(session, make_user, monkeypatch):
    key = await seed_ended_event(session, ENDED)
    user = await subscriber(make_user, session, shows=(ENDED,))
    old = datetime.now(UTC) - timedelta(days=91)
    recent = datetime.now(UTC) - timedelta(days=89)
    for when, label in ((old, "old"), (recent, "recent")):
        session.add(
            PushDelivery(
                user_id=user.id,
                notification_key=label,
                kind="test",
                status="sent",
                created_at=when,
            )
        )
        session.add(m.ShowEvent(kind="ended", show_id=ENDED, new_value=label, observed_at=when))
    await session.commit()
    stub_send(monkeypatch)

    assert await _run_task() is True

    keys = {r.notification_key for r in await deliveries(session)}
    assert keys == {"recent", key}
    session.expire_all()
    events = (await session.execute(select(m.ShowEvent.new_value))).scalars().all()
    assert sorted(events) == ["Ended", "recent"]


# --- the exit code ------------------------------------------------------------


async def test_every_send_failing_fails_the_run(session, make_user, monkeypatch):
    await seed_ended_event(session, ENDED)
    await subscriber(make_user, session, "phone", "laptop", shows=(ENDED,))
    stub_send(monkeypatch, sender.Failed(status=503, error="unavailable"))

    assert await _run_task() is False

    run = await the_run(session)
    assert run.status == "failed"
    assert run.error is not None and "every send failed" in run.error


async def test_nothing_to_send_succeeds(session, monkeypatch):
    calls = stub_send(monkeypatch)

    assert await _run_task() is True
    assert calls == []
    assert (await the_run(session)).status == "succeeded"


# --- the scheduled-task contract ---------------------------------------------


async def test_a_successful_run_pings_only_its_own_check(session, monkeypatch):
    stub_send(monkeypatch)
    with respx.mock(assert_all_called=False) as router:
        start = router.post(f"{HEALTHCHECK}/start").mock(return_value=httpx.Response(200))
        success = router.post(HEALTHCHECK).mock(return_value=httpx.Response(200))
        other = router.route(host="hc.example.com", path__startswith="/push-airs-today").mock(
            return_value=httpx.Response(200)
        )

        ok = await _run_task(
            healthcheck_push_events_url=HEALTHCHECK,
            healthcheck_push_airs_today_url="https://hc.example.com/push-airs-today",
        )

    assert ok is True
    assert start.called and success.called
    assert not other.called


@respx.mock
async def test_it_refuses_to_start_without_vapid_keys(session, monkeypatch):
    calls = stub_send(monkeypatch)
    start = respx.post(f"{HEALTHCHECK}/start").mock(return_value=httpx.Response(200))
    fail = respx.post(f"{HEALTHCHECK}/fail").mock(return_value=httpx.Response(200))

    ok = await _run_task(vapid_subject=None, healthcheck_push_events_url=HEALTHCHECK)

    assert ok is False
    assert fail.called and not start.called
    assert calls == []
    assert (await session.execute(select(m.IngestRun))).first() is None


async def test_a_run_already_in_flight_is_left_alone(session, monkeypatch):
    calls = stub_send(monkeypatch)
    await create_run(session, kind="push_events")
    await session.commit()

    assert await _run_task() is True
    assert calls == []


async def test_an_airs_today_run_in_flight_does_not_block_it(session, make_user, monkeypatch):
    """AC 4: the in-flight guard is per kind."""
    await seed_ended_event(session, ENDED)
    await subscriber(make_user, session, shows=(ENDED,))
    await create_run(session, kind="push_airs_today")
    await session.commit()
    calls = stub_send(monkeypatch)

    assert await _run_task() is True

    assert len(calls) == 1


async def test_the_purge_leaves_todays_airs_today_rows_alone(session, make_user, monkeypatch):
    """The purge is by age, not by task: a row the airs-today task logged this
    morning survives the events task's purge this afternoon."""
    await seed_airs_today(session)
    await subscriber(make_user, session)
    [subscription] = await subscriptions(session)
    session.add(
        PushDelivery(
            subscription_id=subscription.id,
            user_id=subscription.user_id,
            notification_key=airs_today_key(),
            kind="airs_today",
            status="sent",
        )
    )
    await session.commit()
    stub_send(monkeypatch)

    assert await _run_task() is True

    assert [r.notification_key for r in await deliveries(session)] == [airs_today_key()]
