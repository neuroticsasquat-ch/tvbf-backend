"""The daily airs-today push, end to end against the test database (NEU-1540, spec §1).

Which candidates exist is `tests/integration/push/test_candidates.py`'s business;
here there is one airs-today episode, enough to drive every delivery rule, which
the events task shares (`test_push_events_cli.py`).
"""

from datetime import UTC, datetime, timedelta

import httpx
import respx
from sqlalchemy import select, update

from tests.fixtures.push_delivery import (
    EPISODE,
    SEASON,
    SHOW,
    airs_today_key,
    deliveries,
    push_settings,
    seed_airs_today,
    seed_another_show_airing,
    seed_ended_event,
    stub_send,
    stub_send_deleting,
    subscriber,
    subscriptions,
    the_run,
    today,
)
from tvbf.app.models import PushDelivery
from tvbf.catalog import models as m
from tvbf.catalog.runs import create_run
from tvbf.jobs import push_airs_today
from tvbf.push import sender

HEALTHCHECK = "https://hc.example.com/push-airs-today"


async def _run_task(**overrides) -> bool:
    return await push_airs_today.run_push_airs_today(push_settings(**overrides))


# --- delivery ---------------------------------------------------------------


async def test_a_candidate_is_sent_to_every_device_and_logged(session, make_user, monkeypatch):
    await seed_airs_today(session)
    user_id = (await subscriber(make_user, session, "phone", "laptop")).id
    calls = stub_send(monkeypatch)

    assert await _run_task() is True

    assert len(calls) == 2
    payload = calls[0][1]
    assert payload["key"] == airs_today_key()
    assert payload["body"] == "S2E4 “Woe’s Hollow” airs today"
    run = await the_run(session)
    assert (run.kind, run.status, run.shows_processed) == ("push_airs_today", "succeeded", 2)
    run_id = run.id
    rows = await deliveries(session)
    assert [(r.status, r.status_code, r.kind, r.show_id) for r in rows] == [
        ("sent", 201, "airs_today", SHOW)
    ] * 2
    assert all(r.user_id == user_id and r.run_id == run_id and r.sent_at for r in rows)
    assert all(s.last_success_at is not None for s in await subscriptions(session))


async def test_catalog_events_are_not_this_tasks_to_send(session, make_user, monkeypatch):
    await seed_airs_today(session)
    await seed_ended_event(session, SHOW + 1)
    await subscriber(make_user, session, shows=(SHOW, SHOW + 1))
    calls = stub_send(monkeypatch)

    assert await _run_task() is True

    assert [payload["key"] for _, payload in calls] == [airs_today_key()]


async def test_there_is_no_cap_by_default(session, make_user, monkeypatch):
    """Seven shows airing today is seven pushes and no summary (NEU-1540)."""
    await seed_airs_today(session)
    for show_id in range(SHOW + 1, SHOW + 7):
        await seed_another_show_airing(session, show_id, name=f"Show {show_id}")
    await subscriber(make_user, session, shows=tuple(range(SHOW, SHOW + 7)))
    calls = stub_send(monkeypatch)

    assert await _run_task() is True

    assert len(calls) == 7
    assert all(payload["kind"] == "airs_today" for _, payload in calls)


async def test_past_its_cap_the_rest_folds_into_its_own_summary(session, make_user, monkeypatch):
    """AC 3's airs-today half: a cap of 2 and three shows airing."""
    await seed_airs_today(session)
    await seed_another_show_airing(session, SHOW + 1, name="Andor")
    await seed_another_show_airing(session, SHOW + 2, name="The Wire")
    user = await subscriber(make_user, session, shows=(SHOW, SHOW + 1, SHOW + 2))
    calls = stub_send(monkeypatch)

    assert await _run_task(push_airs_today_daily_cap=2) is True

    summary_key = f"summary:airs_today:{user.id}:{today().isoformat()}"
    assert [payload["key"] for _, payload in calls] == [
        airs_today_key(SHOW + 1),
        airs_today_key(),
        summary_key,
    ]
    assert calls[-1][1]["title"] == "1 more show airs today"
    assert calls[-1][1]["body"] == "The Wire"
    rows = await deliveries(session)
    assert [(r.notification_key, r.kind, r.status) for r in rows][-1] == (
        summary_key,
        "summary",
        "sent",
    )


async def test_a_second_run_sends_nothing_again(session, make_user, monkeypatch):
    """The idempotency rule: the unique `(notification_key, subscription_id)`
    finds the `sent` row and the pair is skipped."""
    await seed_airs_today(session)
    await subscriber(make_user, session)
    calls = stub_send(monkeypatch)

    assert await _run_task() is True
    assert await _run_task() is True

    assert len(calls) == 1
    assert [r.status for r in await deliveries(session)] == ["sent"]


async def test_a_stale_pending_row_from_a_crashed_run_is_resent(session, make_user, monkeypatch):
    """A crash between claim and send leaves `pending`; an hour later it is
    treated as failed and the same row is re-claimed and sent (§4.3)."""
    await seed_airs_today(session)
    await subscriber(make_user, session)
    [subscription] = await subscriptions(session)
    crashed = await create_run(session, kind="push_airs_today")
    session.add(
        PushDelivery(
            subscription_id=subscription.id,
            user_id=subscription.user_id,
            notification_key=airs_today_key(),
            kind="airs_today",
            status="pending",
            run_id=crashed,
            created_at=datetime.now(UTC) - timedelta(hours=2),
        )
    )
    await session.execute(
        update(m.IngestRun).where(m.IngestRun.id == crashed).values(status="failed")
    )
    await session.commit()
    calls = stub_send(monkeypatch)

    assert await _run_task() is True

    assert len(calls) == 1
    [row] = await deliveries(session)
    assert row.status == "sent"
    assert row.run_id != crashed


async def test_a_fresh_pending_row_is_left_alone(session, make_user, monkeypatch):
    """Younger than an hour it may be a send still in flight."""
    await seed_airs_today(session)
    await subscriber(make_user, session)
    [subscription] = await subscriptions(session)
    session.add(
        PushDelivery(
            subscription_id=subscription.id,
            user_id=subscription.user_id,
            notification_key=airs_today_key(),
            kind="airs_today",
            status="pending",
        )
    )
    await session.commit()
    calls = stub_send(monkeypatch)

    assert await _run_task() is True

    assert calls == []
    assert [r.status for r in await deliveries(session)] == ["pending"]


async def test_a_failed_send_is_retried_on_the_next_run(session, make_user, monkeypatch):
    await seed_airs_today(session)
    await subscriber(make_user, session)
    stub_send(monkeypatch, sender.Failed(status=500, error="boom"))
    await _run_task()
    calls = stub_send(monkeypatch)

    assert await _run_task() is True

    assert len(calls) == 1
    [row] = await deliveries(session)
    assert (row.status, row.error) == ("sent", None)
    [subscription] = await subscriptions(session)
    assert subscription.failure_count == 0


# --- retirement -------------------------------------------------------------


async def test_a_gone_subscription_is_retired_and_its_row_survives(session, make_user, monkeypatch):
    """404/410 deletes the subscription at once; the delivery row outlives it
    with `subscription_id` nulled — what the admin stats count as retired."""
    await seed_airs_today(session)
    user_id = (await subscriber(make_user, session, "phone", "laptop")).id
    calls = stub_send(
        monkeypatch,
        lambda endpoint: (
            sender.Gone(status=410) if endpoint.endswith("phone") else sender.Sent(status=201)
        ),
    )

    assert await _run_task() is True

    assert len(calls) == 2
    assert [s.endpoint for s in await subscriptions(session)] == ["https://push.example/laptop"]
    rows = await deliveries(session)
    gone = next(r for r in rows if r.status == "failed")
    assert (gone.error, gone.status_code, gone.subscription_id) == ("gone", 410, None)
    assert gone.user_id == user_id


async def test_a_retired_subscription_gets_no_further_candidates(session, make_user, monkeypatch):
    """Two candidates, one device answering 410 to the first: the second is
    never attempted against a subscription that no longer exists."""
    await seed_airs_today(session)
    await seed_another_show_airing(session)
    await subscriber(make_user, session, shows=(SHOW, SHOW + 1))
    calls = stub_send(monkeypatch, sender.Gone(status=410))

    # And a day whose only sends hit dead devices is not an outage.
    assert await _run_task() is True

    assert len(calls) == 1


async def test_a_season_dump_is_one_push(session, make_user, monkeypatch):
    """NEU-1539's case: eight episodes of one show today and one of another —
    two pushes, and the drop's push names the range."""
    await seed_airs_today(session)
    for n in range(1, 8):
        session.add(
            m.Episode(
                id=EPISODE + 10 + n,
                tmdb_id=EPISODE + 10 + n,
                show_id=SHOW,
                season_id=SEASON,
                season_number=2,
                episode_number=4 + n,
                air_date=today(),
            )
        )
    await session.commit()
    await seed_another_show_airing(session)
    await subscriber(make_user, session, shows=(SHOW, SHOW + 1))
    calls = stub_send(monkeypatch)

    assert await _run_task() is True

    payloads = {payload["key"]: payload for _, payload in calls}
    assert set(payloads) == {airs_today_key(), airs_today_key(SHOW + 1)}
    assert payloads[airs_today_key()]["body"] == "8 episodes air today (S2E4–E11)"
    assert payloads[airs_today_key()]["url"] == f"/shows/{SHOW}/episodes?season=2"
    # Andor's one episode, then Severance's eight — two rows.
    assert [(row.notification_key, row.kind, row.status) for row in await deliveries(session)] == [
        (airs_today_key(SHOW + 1), "airs_today", "sent"),
        (airs_today_key(), "airs_today", "sent"),
    ]


async def test_a_subscription_deleted_mid_run_does_not_abort_it(session, make_user, monkeypatch):
    """The user turns a device off while the job is sending to it: the next
    claim against it would violate the delivery log's FK. That device is
    dropped; the run goes on."""
    await seed_airs_today(session)
    await seed_another_show_airing(session)
    await subscriber(make_user, session, shows=(SHOW, SHOW + 1))
    calls = stub_send_deleting(monkeypatch, sender.Sent(status=201))

    assert await _run_task() is True

    assert len(calls) == 1
    assert (await the_run(session)).status == "succeeded"


async def test_a_failure_against_a_deleted_subscription_does_not_abort_the_run(
    session, make_user, monkeypatch
):
    await seed_airs_today(session)
    await seed_another_show_airing(session)
    await subscriber(make_user, session, shows=(SHOW, SHOW + 1))
    calls = stub_send_deleting(monkeypatch, sender.Failed(status=500, error="boom"))

    assert await _run_task() is False  # its only send failed

    assert len(calls) == 1
    [row] = await deliveries(session)
    assert (row.status, row.error, row.subscription_id) == ("failed", "boom", None)
    assert "every send failed" in ((await the_run(session)).error or "")


async def test_the_fifth_consecutive_failure_retires_the_subscription(
    session, make_user, monkeypatch
):
    await seed_airs_today(session)
    await subscriber(make_user, session, failure_count=4)
    stub_send(monkeypatch, sender.Failed(status=500, error="boom"))

    await _run_task()

    assert await subscriptions(session) == []
    [row] = await deliveries(session)
    assert (row.status, row.status_code, row.error, row.subscription_id) == (
        "failed",
        500,
        "failure_limit",
        None,
    )


async def test_a_failure_below_the_limit_counts_but_keeps_the_subscription(
    session, make_user, monkeypatch
):
    await seed_airs_today(session)
    await subscriber(make_user, session, failure_count=2)
    stub_send(monkeypatch, sender.Failed(status=None, error="ConnectionError: refused"))

    await _run_task()

    [subscription] = await subscriptions(session)
    assert subscription.failure_count == 3
    [row] = await deliveries(session)
    assert (row.status, row.error) == ("failed", "ConnectionError: refused")


# --- purge ------------------------------------------------------------------


async def test_it_purges_nothing(session, make_user, monkeypatch):
    """The events task owns the 90-day purge (NEU-1540)."""
    await seed_airs_today(session)
    user = await subscriber(make_user, session)
    old = datetime.now(UTC) - timedelta(days=91)
    session.add(
        PushDelivery(
            user_id=user.id, notification_key="old", kind="test", status="sent", created_at=old
        )
    )
    session.add(m.ShowEvent(kind="ended", show_id=SHOW, new_value="old", observed_at=old))
    await session.commit()
    stub_send(monkeypatch)

    assert await _run_task() is True

    assert {r.notification_key for r in await deliveries(session)} == {"old", airs_today_key()}
    session.expire_all()
    assert (await session.execute(select(m.ShowEvent.new_value))).scalars().all() == ["old"]


# --- the exit code ------------------------------------------------------------


async def test_every_send_failing_fails_the_run(session, make_user, monkeypatch):
    """A push service outage (or a bad key) is Coolify's business."""
    await seed_airs_today(session)
    await subscriber(make_user, session, "phone", "laptop")
    stub_send(monkeypatch, sender.Failed(status=503, error="unavailable"))

    assert await _run_task() is False

    run = await the_run(session)
    assert run.status == "failed"
    assert run.error is not None and "every send failed" in run.error


async def test_some_sends_failing_still_succeeds(session, make_user, monkeypatch):
    """One bad device is the log's business, never the exit code's."""
    await seed_airs_today(session)
    await subscriber(make_user, session, "phone", "laptop")
    stub_send(
        monkeypatch,
        lambda endpoint: (
            sender.Failed(status=500, error="boom")
            if endpoint.endswith("phone")
            else sender.Sent(status=201)
        ),
    )

    assert await _run_task() is True
    assert (await the_run(session)).shows_failed == 1


async def test_nothing_to_send_succeeds(session, monkeypatch):
    calls = stub_send(monkeypatch)

    assert await _run_task() is True
    assert calls == []
    assert (await the_run(session)).status == "succeeded"


async def test_a_crash_finalizes_the_run_failed(session, make_user, monkeypatch):
    await seed_airs_today(session)
    await subscriber(make_user, session)

    async def _boom(*args, **kwargs):
        raise RuntimeError("push service client exploded")

    monkeypatch.setattr(sender, "send", _boom)

    assert await _run_task() is False
    run = await the_run(session)
    assert (run.status, run.error) == ("failed", "push service client exploded")


# --- the scheduled-task contract ---------------------------------------------


async def test_a_successful_run_pings_only_its_own_check(session, monkeypatch):
    stub_send(monkeypatch)
    with respx.mock(assert_all_called=False) as router:
        start = router.post(f"{HEALTHCHECK}/start").mock(return_value=httpx.Response(200))
        success = router.post(HEALTHCHECK).mock(return_value=httpx.Response(200))
        other = router.route(host="hc.example.com", path__startswith="/push-events").mock(
            return_value=httpx.Response(200)
        )

        ok = await _run_task(
            healthcheck_push_airs_today_url=HEALTHCHECK,
            healthcheck_push_events_url="https://hc.example.com/push-events",
        )

    assert ok is True
    assert start.called and success.called
    assert not other.called


@respx.mock
async def test_it_refuses_to_start_without_vapid_keys(session, monkeypatch):
    """Exit 1, `/fail`, and no run row: a job that cannot sign a push must not
    pass for a quiet success on a day nobody happens to be subscribed."""
    calls = stub_send(monkeypatch)
    start = respx.post(f"{HEALTHCHECK}/start").mock(return_value=httpx.Response(200))
    fail = respx.post(f"{HEALTHCHECK}/fail").mock(return_value=httpx.Response(200))

    ok = await _run_task(vapid_private_key=None, healthcheck_push_airs_today_url=HEALTHCHECK)

    assert ok is False
    assert fail.called and not start.called
    assert calls == []
    assert (await session.execute(select(m.IngestRun))).first() is None


async def test_a_run_already_in_flight_is_left_alone(session, monkeypatch):
    calls = stub_send(monkeypatch)
    await create_run(session, kind="push_airs_today")
    await session.commit()

    assert await _run_task() is True
    assert calls == []


async def test_an_events_run_in_flight_does_not_block_it(session, make_user, monkeypatch):
    """AC 4: the in-flight guard is per kind."""
    await seed_airs_today(session)
    await subscriber(make_user, session)
    await create_run(session, kind="push_events")
    await session.commit()
    calls = stub_send(monkeypatch)

    assert await _run_task() is True

    assert len(calls) == 1
