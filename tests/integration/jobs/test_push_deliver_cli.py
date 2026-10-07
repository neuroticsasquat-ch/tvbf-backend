"""The daily push delivery, end to end against the test database (NEU-1489, spec §5.2).

`sender.send` is stubbed — the module boundary spec §8 names — so everything
else is real: the candidate queries, the cap, the delivery log's claim and
retry rule, retirement, the purge, the run row and the exit code. Which
candidates exist is `tests/integration/push/test_candidates.py`'s business;
here there is one airs-today episode, enough to drive every delivery rule.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import respx
from sqlalchemy import delete, select, update

from tvbf.app.models import PushDelivery, PushSubscription, UserShowWatch
from tvbf.catalog import models as m
from tvbf.catalog.runs import create_run
from tvbf.config import get_settings
from tvbf.db import SessionLocal
from tvbf.jobs import push_deliver
from tvbf.push import sender

HEALTHCHECK = "https://hc.example.com/push"
SHOW, SEASON, EPISODE = 1, 10, 100


def _settings(**overrides):
    return get_settings().model_copy(
        update={
            "vapid_private_key": "private",
            "vapid_public_key": "public",
            "vapid_subject": "mailto:ops@example.com",
            **overrides,
        }
    )


def _today():
    return datetime.now(UTC).date()


def _key(show_id: int = SHOW) -> str:
    return f"airs_today:{show_id}:{_today().isoformat()}"


async def _airs_today(session) -> None:
    session.add(m.Show(id=SHOW, tmdb_id=SHOW, name="Severance", poster_path="/p.jpg"))
    await session.flush()
    session.add(m.Season(id=SEASON, tmdb_id=SEASON, show_id=SHOW, season_number=2))
    await session.flush()
    session.add(
        m.Episode(
            id=EPISODE,
            tmdb_id=EPISODE,
            show_id=SHOW,
            season_id=SEASON,
            season_number=2,
            episode_number=4,
            name="Woe’s Hollow",
            air_date=_today(),
        )
    )
    await session.commit()


async def _subscriber(
    make_user, session, *devices: str, failure_count: int = 0, shows: tuple[int, ...] = (SHOW,)
):
    user = await make_user(email="a@example.com")
    for show_id in shows:
        session.add(UserShowWatch(user_id=user.id, show_id=show_id))
    for device in devices or ("phone",):
        session.add(
            PushSubscription(
                user_id=user.id,
                endpoint=f"https://push.example/{device}",
                p256dh="k",
                auth="a",
                failure_count=failure_count,
            )
        )
    await session.commit()
    return user


def _stub_send(
    monkeypatch,
    outcome: sender.SendOutcome | Callable[[str], sender.SendOutcome] = sender.Sent(status=201),
):
    """Record every send; answer each with `outcome` (or `outcome(endpoint)`)."""
    calls: list[tuple[str, dict]] = []

    async def _send(subscription, payload, *, ttl=sender.DEFAULT_TTL_SECONDS):
        calls.append((subscription.endpoint, payload))
        assert ttl == 86400
        return outcome(subscription.endpoint) if callable(outcome) else outcome

    monkeypatch.setattr(sender, "send", _send)
    return calls


async def _deliveries(session) -> list[PushDelivery]:
    session.expire_all()
    return list((await session.execute(select(PushDelivery).order_by(PushDelivery.id))).scalars())


async def _subscriptions(session) -> list[PushSubscription]:
    session.expire_all()
    return list((await session.execute(select(PushSubscription))).scalars())


async def _run(session) -> m.IngestRun:
    session.expire_all()
    return (await session.execute(select(m.IngestRun))).scalar_one()


# --- delivery ---------------------------------------------------------------


async def test_a_candidate_is_sent_to_every_device_and_logged(session, make_user, monkeypatch):
    await _airs_today(session)
    user_id = (await _subscriber(make_user, session, "phone", "laptop")).id
    calls = _stub_send(monkeypatch)

    assert await push_deliver.run_push_daily(_settings()) is True

    assert len(calls) == 2
    payload = calls[0][1]
    assert payload["key"] == _key()
    assert payload["body"] == "S2E4 “Woe’s Hollow” airs today"
    run = await _run(session)
    assert (run.kind, run.status, run.shows_processed) == ("push_deliver", "succeeded", 2)
    run_id = run.id
    rows = await _deliveries(session)
    assert [(r.status, r.status_code, r.kind, r.show_id) for r in rows] == [
        ("sent", 201, "airs_today", SHOW)
    ] * 2
    assert all(r.user_id == user_id and r.run_id == run_id and r.sent_at for r in rows)
    assert all(s.last_success_at is not None for s in await _subscriptions(session))


async def test_a_second_run_sends_nothing_again(session, make_user, monkeypatch):
    """The idempotency rule: the unique `(notification_key, subscription_id)`
    finds the `sent` row and the pair is skipped."""
    await _airs_today(session)
    await _subscriber(make_user, session)
    calls = _stub_send(monkeypatch)

    assert await push_deliver.run_push_daily(_settings()) is True
    assert await push_deliver.run_push_daily(_settings()) is True

    assert len(calls) == 1
    assert [r.status for r in await _deliveries(session)] == ["sent"]


async def test_a_stale_pending_row_from_a_crashed_run_is_resent(session, make_user, monkeypatch):
    """A crash between claim and send leaves `pending`; an hour later it is
    treated as failed and the same row is re-claimed and sent (§4.3)."""
    await _airs_today(session)
    await _subscriber(make_user, session)
    [subscription] = await _subscriptions(session)
    crashed = await create_run(session, kind="push_deliver")
    session.add(
        PushDelivery(
            subscription_id=subscription.id,
            user_id=subscription.user_id,
            notification_key=_key(),
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
    calls = _stub_send(monkeypatch)

    assert await push_deliver.run_push_daily(_settings()) is True

    assert len(calls) == 1
    [row] = await _deliveries(session)
    assert row.status == "sent"
    assert row.run_id != crashed


async def test_a_fresh_pending_row_is_left_alone(session, make_user, monkeypatch):
    """Younger than an hour it may be a send still in flight."""
    await _airs_today(session)
    await _subscriber(make_user, session)
    [subscription] = await _subscriptions(session)
    session.add(
        PushDelivery(
            subscription_id=subscription.id,
            user_id=subscription.user_id,
            notification_key=_key(),
            kind="airs_today",
            status="pending",
        )
    )
    await session.commit()
    calls = _stub_send(monkeypatch)

    assert await push_deliver.run_push_daily(_settings()) is True

    assert calls == []
    assert [r.status for r in await _deliveries(session)] == ["pending"]


async def test_a_failed_send_is_retried_on_the_next_run(session, make_user, monkeypatch):
    await _airs_today(session)
    await _subscriber(make_user, session)
    _stub_send(monkeypatch, sender.Failed(status=500, error="boom"))
    await push_deliver.run_push_daily(_settings())
    calls = _stub_send(monkeypatch)

    assert await push_deliver.run_push_daily(_settings()) is True

    assert len(calls) == 1
    [row] = await _deliveries(session)
    assert (row.status, row.error) == ("sent", None)
    [subscription] = await _subscriptions(session)
    assert subscription.failure_count == 0


# --- retirement -------------------------------------------------------------


async def test_a_gone_subscription_is_retired_and_its_row_survives(session, make_user, monkeypatch):
    """404/410 deletes the subscription at once; the delivery row outlives it
    with `subscription_id` nulled — what the admin stats count as retired."""
    await _airs_today(session)
    user_id = (await _subscriber(make_user, session, "phone", "laptop")).id
    calls = _stub_send(
        monkeypatch,
        lambda endpoint: (
            sender.Gone(status=410) if endpoint.endswith("phone") else sender.Sent(status=201)
        ),
    )

    assert await push_deliver.run_push_daily(_settings()) is True

    assert len(calls) == 2
    assert [s.endpoint for s in await _subscriptions(session)] == ["https://push.example/laptop"]
    rows = await _deliveries(session)
    gone = next(r for r in rows if r.status == "failed")
    assert (gone.error, gone.status_code, gone.subscription_id) == ("gone", 410, None)
    assert gone.user_id == user_id


async def test_a_retired_subscription_gets_no_further_candidates(session, make_user, monkeypatch):
    """Two candidates, one device answering 410 to the first: the second is
    never attempted against a subscription that no longer exists."""
    await _airs_today(session)
    await _second_show(session)
    await _subscriber(make_user, session, shows=(SHOW, SHOW + 1))
    calls = _stub_send(monkeypatch, sender.Gone(status=410))

    # And a day whose only sends hit dead devices is not an outage.
    assert await push_deliver.run_push_daily(_settings()) is True

    assert len(calls) == 1


async def _second_show(session, *, status: str = "Returning Series") -> None:
    """A second tracked show with its own episode today — a second *candidate*.
    A second episode of the first show would fold into its one push (NEU-1539)."""
    session.add(m.Show(id=SHOW + 1, tmdb_id=SHOW + 1, name="Andor", status=status))
    await session.flush()
    session.add(m.Season(id=SEASON + 1, tmdb_id=SEASON + 1, show_id=SHOW + 1, season_number=1))
    await session.flush()
    session.add(
        m.Episode(
            id=EPISODE + 1,
            tmdb_id=EPISODE + 1,
            show_id=SHOW + 1,
            season_id=SEASON + 1,
            season_number=1,
            episode_number=1,
            air_date=_today(),
        )
    )
    await session.commit()


async def _season_dump(session, count: int) -> None:
    """`count` more episodes of the first show, all airing today."""
    for n in range(1, count + 1):
        session.add(
            m.Episode(
                id=EPISODE + 10 + n,
                tmdb_id=EPISODE + 10 + n,
                show_id=SHOW,
                season_id=SEASON,
                season_number=2,
                episode_number=4 + n,
                air_date=_today(),
            )
        )
    await session.commit()


async def test_a_season_dump_is_one_push_and_spends_one_cap_slot(session, make_user, monkeypatch):
    """The ticket's case (NEU-1539): eight episodes of one show today and an
    `ended` event on another — two pushes, no summary, and the drop's push
    names the range."""
    await _airs_today(session)
    await _season_dump(session, 7)
    await _second_show(session, status="Ended")
    event = m.ShowEvent(kind="ended", show_id=SHOW + 1, new_value="Ended")
    session.add(event)
    await session.commit()
    ended = f"ended:{event.id}"
    await _subscriber(make_user, session, shows=(SHOW, SHOW + 1))
    calls = _stub_send(monkeypatch)

    assert await push_deliver.run_push_daily(_settings(push_daily_cap=5)) is True

    payloads = {payload["key"]: payload for _, payload in calls}
    assert set(payloads) == {_key(), _key(SHOW + 1), ended}
    assert payloads[_key()]["body"] == "8 episodes air today (S2E4–E11)"
    assert payloads[_key()]["url"] == f"/shows/{SHOW}/episodes?season=2"
    # Andor's one episode, Severance's eight, then the event — three rows, no summary.
    assert [(row.notification_key, row.kind, row.status) for row in await _deliveries(session)] == [
        (_key(SHOW + 1), "airs_today", "sent"),
        (_key(), "airs_today", "sent"),
        (ended, "ended", "sent"),
    ]


def _stub_send_deleting(monkeypatch, outcome: sender.SendOutcome) -> list[str]:
    """A send during which the user deletes that very subscription."""
    calls: list[str] = []

    async def _send(subscription, payload, *, ttl=sender.DEFAULT_TTL_SECONDS):
        calls.append(subscription.endpoint)
        async with SessionLocal() as s:
            await s.execute(
                delete(PushSubscription).where(PushSubscription.endpoint == subscription.endpoint)
            )
            await s.commit()
        return outcome

    monkeypatch.setattr(sender, "send", _send)
    return calls


async def test_a_subscription_deleted_mid_run_does_not_abort_it(session, make_user, monkeypatch):
    """The user turns a device off while the job is sending to it: the next
    claim against it would violate the delivery log's FK. That device is
    dropped; the run goes on."""
    await _airs_today(session)
    await _second_show(session)
    await _subscriber(make_user, session, shows=(SHOW, SHOW + 1))
    calls = _stub_send_deleting(monkeypatch, sender.Sent(status=201))

    assert await push_deliver.run_push_daily(_settings()) is True

    assert len(calls) == 1
    assert (await _run(session)).status == "succeeded"


async def test_a_failure_against_a_deleted_subscription_does_not_abort_the_run(
    session, make_user, monkeypatch
):
    await _airs_today(session)
    await _second_show(session)
    await _subscriber(make_user, session, shows=(SHOW, SHOW + 1))
    calls = _stub_send_deleting(monkeypatch, sender.Failed(status=500, error="boom"))

    assert await push_deliver.run_push_daily(_settings()) is False  # its only send failed

    assert len(calls) == 1
    [row] = await _deliveries(session)
    assert (row.status, row.error, row.subscription_id) == ("failed", "boom", None)
    assert "every send failed" in ((await _run(session)).error or "")


async def test_the_fifth_consecutive_failure_retires_the_subscription(
    session, make_user, monkeypatch
):
    await _airs_today(session)
    await _subscriber(make_user, session, failure_count=4)
    _stub_send(monkeypatch, sender.Failed(status=500, error="boom"))

    await push_deliver.run_push_daily(_settings())

    assert await _subscriptions(session) == []
    [row] = await _deliveries(session)
    assert (row.status, row.status_code, row.error, row.subscription_id) == (
        "failed",
        500,
        "failure_limit",
        None,
    )


async def test_a_failure_below_the_limit_counts_but_keeps_the_subscription(
    session, make_user, monkeypatch
):
    await _airs_today(session)
    await _subscriber(make_user, session, failure_count=2)
    _stub_send(monkeypatch, sender.Failed(status=None, error="ConnectionError: refused"))

    await push_deliver.run_push_daily(_settings())

    [subscription] = await _subscriptions(session)
    assert subscription.failure_count == 3
    [row] = await _deliveries(session)
    assert (row.status, row.error) == ("failed", "ConnectionError: refused")


# --- purge ------------------------------------------------------------------


async def test_rows_older_than_ninety_days_are_purged(session, make_user, monkeypatch):
    await _airs_today(session)
    user = await _subscriber(make_user, session)
    old = datetime.now(UTC) - timedelta(days=91)
    recent = datetime.now(UTC) - timedelta(days=89)
    for when, key in ((old, "old"), (recent, "recent")):
        session.add(
            PushDelivery(
                user_id=user.id,
                notification_key=key,
                kind="test",
                status="sent",
                created_at=when,
            )
        )
        session.add(m.ShowEvent(kind="ended", show_id=SHOW, new_value=key, observed_at=when))
    await session.commit()
    _stub_send(monkeypatch)

    assert await push_deliver.run_push_daily(_settings()) is True

    keys = {r.notification_key for r in await _deliveries(session)}
    assert keys == {"recent", _key()}
    session.expire_all()
    events = (await session.execute(select(m.ShowEvent.new_value))).scalars().all()
    assert events == ["recent"]


# --- the exit code ------------------------------------------------------------


async def test_every_send_failing_fails_the_run(session, make_user, monkeypatch):
    """A push service outage (or a bad key) is Coolify's business."""
    await _airs_today(session)
    await _subscriber(make_user, session, "phone", "laptop")
    _stub_send(monkeypatch, sender.Failed(status=503, error="unavailable"))

    assert await push_deliver.run_push_daily(_settings()) is False

    run = await _run(session)
    assert run.status == "failed"
    assert run.error is not None and "every send failed" in run.error


async def test_some_sends_failing_still_succeeds(session, make_user, monkeypatch):
    """One bad device is the log's business, never the exit code's."""
    await _airs_today(session)
    await _subscriber(make_user, session, "phone", "laptop")
    _stub_send(
        monkeypatch,
        lambda endpoint: (
            sender.Failed(status=500, error="boom")
            if endpoint.endswith("phone")
            else sender.Sent(status=201)
        ),
    )

    assert await push_deliver.run_push_daily(_settings()) is True
    assert (await _run(session)).shows_failed == 1


async def test_nothing_to_send_succeeds(session, monkeypatch):
    calls = _stub_send(monkeypatch)

    assert await push_deliver.run_push_daily(_settings()) is True
    assert calls == []
    assert (await _run(session)).status == "succeeded"


async def test_a_crash_finalizes_the_run_failed(session, make_user, monkeypatch):
    await _airs_today(session)
    await _subscriber(make_user, session)

    async def _boom(*args, **kwargs):
        raise RuntimeError("push service client exploded")

    monkeypatch.setattr(sender, "send", _boom)

    assert await push_deliver.run_push_daily(_settings()) is False
    run = await _run(session)
    assert (run.status, run.error) == ("failed", "push service client exploded")


# --- the scheduled-task contract ---------------------------------------------


@respx.mock
async def test_a_successful_run_pings_its_own_check(session, monkeypatch):
    _stub_send(monkeypatch)
    start = respx.post(f"{HEALTHCHECK}/start").mock(return_value=httpx.Response(200))
    success = respx.post(HEALTHCHECK).mock(return_value=httpx.Response(200))

    assert await push_deliver.run_push_daily(_settings(healthcheck_push_url=HEALTHCHECK)) is True
    assert start.called and success.called


@respx.mock
async def test_it_refuses_to_start_without_vapid_keys(session, monkeypatch):
    """Exit 1, `/fail`, and no run row: a job that cannot sign a push must not
    pass for a quiet success on a day nobody happens to be subscribed."""
    calls = _stub_send(monkeypatch)
    start = respx.post(f"{HEALTHCHECK}/start").mock(return_value=httpx.Response(200))
    fail = respx.post(f"{HEALTHCHECK}/fail").mock(return_value=httpx.Response(200))

    ok = await push_deliver.run_push_daily(
        _settings(vapid_private_key=None, healthcheck_push_url=HEALTHCHECK)
    )

    assert ok is False
    assert fail.called and not start.called
    assert calls == []
    assert (await session.execute(select(m.IngestRun))).first() is None


async def test_a_run_already_in_flight_is_left_alone(session, monkeypatch):
    calls = _stub_send(monkeypatch)
    await create_run(session, kind="push_deliver")
    await session.commit()

    assert await push_deliver.run_push_daily(_settings()) is True
    assert calls == []
