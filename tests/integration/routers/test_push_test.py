"""POST /me/push/test — send a test push to one of my devices (NEU-1486).

`pywebpush` is never reached: `tvbf.push.sender.send` is the seam (spec §8).
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update

from tests.fixtures.users import _client_for
from tvbf.app.models import PushDelivery, PushSubscription
from tvbf.app.repos import push_delivery_repo
from tvbf.app.services import push_test_service
from tvbf.config import get_settings
from tvbf.main import app
from tvbf.push import sender

ENDPOINT = "https://fcm.googleapis.com/fcm/send/abc123"


@pytest.fixture(autouse=True)
def vapid(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "vapid_private_key", "priv")
    monkeypatch.setattr(settings, "vapid_public_key", "BPubKey")
    monkeypatch.setattr(settings, "vapid_subject", "mailto:ops@example.com")
    return settings


class FakeSend:
    """Stands in for `sender.send`, recording each call."""

    def __init__(self, outcome: sender.SendOutcome) -> None:
        self.outcome = outcome
        self.calls: list[tuple[sender.SubscriptionKeys, dict[str, Any]]] = []

    async def __call__(self, subscription, payload, **_kwargs) -> sender.SendOutcome:
        self.calls.append((subscription, payload))
        return self.outcome


@pytest.fixture
def fake_send(monkeypatch):
    def install(outcome: sender.SendOutcome) -> FakeSend:
        fake = FakeSend(outcome)
        monkeypatch.setattr(sender, "send", fake)
        return fake

    return install


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        yield c


@pytest.fixture
async def other_client(session, make_user):
    user = await make_user(email="other@example.com", verified=True)
    async for c in _client_for(session, user):
        yield c


async def _subscribe(client, endpoint: str = ENDPOINT) -> UUID:
    r = await client.post(
        "/me/push/subscriptions",
        json={"endpoint": endpoint, "keys": {"p256dh": "BKey", "auth": "secret"}},
    )
    assert r.status_code == 201, r.text
    return UUID(r.json()["id"])


async def _deliveries(session) -> list[PushDelivery]:
    result = await session.execute(
        select(PushDelivery).execution_options(populate_existing=True).order_by(PushDelivery.id)
    )
    return list(result.scalars().all())


async def _subscription(session, sub_id: UUID) -> PushSubscription | None:
    result = await session.execute(
        select(PushSubscription)
        .where(PushSubscription.id == sub_id)
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def _seed_tests(session, *, sub_id: UUID, user_id: UUID, n: int, age: timedelta) -> None:
    for i in range(n):
        await push_delivery_repo.insert_pending(
            session,
            subscription_id=sub_id,
            user_id=user_id,
            notification_key=f"test:{sub_id}:seed{i}",
            kind="test",
        )
    await session.execute(
        update(PushDelivery).values(created_at=datetime.now(UTC) - age, status="sent")
    )
    await session.commit()


async def test_sends_the_test_payload_and_logs_it_sent(authed_client, session, fake_send):
    sub_id = await _subscribe(authed_client)
    await session.execute(update(PushSubscription).values(failure_count=3))
    await session.commit()
    fake = fake_send(sender.Sent(status=201))

    r = await authed_client.post("/me/push/test", json={"subscription_id": str(sub_id)})

    assert r.status_code == 202
    assert r.json() == {"status": "sent", "status_code": 201}

    [(keys, payload)] = fake.calls
    assert keys == sender.SubscriptionKeys(endpoint=ENDPOINT, p256dh="BKey", auth="secret")
    key = payload.pop("key")
    assert key.startswith(f"test:{sub_id}:")
    assert key.rsplit(":", 1)[1].isdigit()
    assert payload == {
        "kind": "test",
        "title": "TV BingeFriend",
        "body": "Notifications are working",
        "url": "/settings",
    }

    [row] = await _deliveries(session)
    assert (row.notification_key, row.kind, row.subscription_id) == (key, "test", sub_id)
    assert (row.user_id, row.show_id) == (authed_client.user.id, None)
    assert (row.status, row.status_code, row.error) == ("sent", 201, None)
    assert row.sent_at is not None

    sub = await _subscription(session, sub_id)
    assert sub is not None
    assert sub.last_success_at is not None
    assert sub.failure_count == 0


async def test_a_failure_is_reported_not_hidden_and_the_device_is_kept(
    authed_client, session, fake_send
):
    sub_id = await _subscribe(authed_client)
    fake_send(sender.Failed(status=500, error="Push failed: 500 Internal Server Error"))

    r = await authed_client.post("/me/push/test", json={"subscription_id": str(sub_id)})

    assert r.status_code == 202
    assert r.json() == {"status": "failed", "status_code": 500}
    [row] = await _deliveries(session)
    assert (row.status, row.status_code, row.sent_at) == ("failed", 500, None)
    assert row.error == "Push failed: 500 Internal Server Error"
    sub = await _subscription(session, sub_id)
    assert sub is not None
    # A user-initiated test does not count towards the job's retirement rule.
    assert (sub.failure_count, sub.last_success_at) == (0, None)


async def test_a_transport_error_reports_no_status_code(authed_client, session, fake_send):
    sub_id = await _subscribe(authed_client)
    fake_send(sender.Failed(status=None, error="ConnectionError: refused"))

    r = await authed_client.post("/me/push/test", json={"subscription_id": str(sub_id)})

    assert r.status_code == 202
    assert r.json() == {"status": "failed", "status_code": None}


@pytest.mark.parametrize("code", [404, 410])
async def test_gone_deletes_the_subscription_and_answers_410(
    authed_client, session, fake_send, code
):
    sub_id = await _subscribe(authed_client)
    fake_send(sender.Gone(status=code))

    r = await authed_client.post("/me/push/test", json={"subscription_id": str(sub_id)})

    assert r.status_code == 410
    assert r.json() == {"status": "gone", "status_code": code}
    assert await _subscription(session, sub_id) is None
    # The log row outlives the subscription it recorded (§4.3).
    [row] = await _deliveries(session)
    assert (row.status, row.status_code, row.error) == ("failed", code, "gone")
    assert (row.subscription_id, row.user_id) == (None, authed_client.user.id)


async def test_someone_elses_subscription_is_404_and_nothing_is_sent(
    authed_client, other_client, session, fake_send
):
    theirs = await _subscribe(other_client)
    fake = fake_send(sender.Sent(status=201))

    r = await authed_client.post("/me/push/test", json={"subscription_id": str(theirs)})

    assert r.status_code == 404
    assert r.json() == {"detail": "not_found"}
    assert fake.calls == []
    assert await _deliveries(session) == []


async def test_an_unknown_subscription_is_404(authed_client, fake_send):
    fake_send(sender.Sent(status=201))
    r = await authed_client.post(
        "/me/push/test", json={"subscription_id": "00000000-0000-0000-0000-000000000000"}
    )
    assert r.status_code == 404


async def test_throttled_past_five_an_hour(authed_client, session, fake_send):
    sub_id = await _subscribe(authed_client)
    await _seed_tests(
        session, sub_id=sub_id, user_id=authed_client.user.id, n=5, age=timedelta(minutes=10)
    )
    fake = fake_send(sender.Sent(status=201))

    r = await authed_client.post("/me/push/test", json={"subscription_id": str(sub_id)})

    assert r.status_code == 429
    assert r.json() == {"detail": "rate_limited"}
    assert r.headers["Retry-After"] == "3600"
    assert fake.calls == []
    assert len(await _deliveries(session)) == 5


async def test_the_throttle_counts_only_the_window(authed_client, session, fake_send):
    sub_id = await _subscribe(authed_client)
    await _seed_tests(
        session, sub_id=sub_id, user_id=authed_client.user.id, n=5, age=timedelta(minutes=61)
    )
    fake_send(sender.Sent(status=201))

    r = await authed_client.post("/me/push/test", json={"subscription_id": str(sub_id)})

    assert r.status_code == 202


async def test_the_throttle_is_per_user(authed_client, other_client, session, fake_send):
    theirs = await _subscribe(other_client, endpoint=f"{ENDPOINT}-theirs")
    await _seed_tests(
        session, sub_id=theirs, user_id=other_client.user.id, n=5, age=timedelta(minutes=1)
    )
    mine = await _subscribe(authed_client)
    fake_send(sender.Sent(status=201))

    r = await authed_client.post("/me/push/test", json={"subscription_id": str(mine)})

    assert r.status_code == 202


async def test_a_retired_device_does_not_refund_the_budget(authed_client, session, fake_send):
    fake_send(sender.Gone(status=410))
    for i in range(5):
        sub_id = await _subscribe(authed_client, endpoint=f"{ENDPOINT}-{i}")
        r = await authed_client.post("/me/push/test", json={"subscription_id": str(sub_id)})
        assert r.status_code == 410

    sub_id = await _subscribe(authed_client)
    r = await authed_client.post("/me/push/test", json={"subscription_id": str(sub_id)})

    assert r.status_code == 429


async def test_a_second_test_in_the_same_second_is_429(
    authed_client, session, fake_send, monkeypatch
):
    sub_id = await _subscribe(authed_client)
    fake = fake_send(sender.Sent(status=201))
    monkeypatch.setattr(push_test_service.time, "time", lambda: 1_790_000_000.5)

    first = await authed_client.post("/me/push/test", json={"subscription_id": str(sub_id)})
    second = await authed_client.post("/me/push/test", json={"subscription_id": str(sub_id)})

    assert first.status_code == 202
    assert second.status_code == 429
    assert second.headers["Retry-After"] == "1"
    assert len(fake.calls) == 1


async def test_is_503_when_vapid_is_not_configured(authed_client, vapid, monkeypatch, fake_send):
    sub_id = await _subscribe(authed_client)
    monkeypatch.setattr(vapid, "vapid_subject", None)
    fake = fake_send(sender.Sent(status=201))

    r = await authed_client.post("/me/push/test", json={"subscription_id": str(sub_id)})

    assert r.status_code == 503
    assert r.json() == {"detail": "vapid_not_configured"}
    assert fake.calls == []


async def test_requires_csrf(authed_client, session, fake_send):
    sub_id = await _subscribe(authed_client)
    fake = fake_send(sender.Sent(status=201))

    r = await authed_client.post(
        "/me/push/test",
        json={"subscription_id": str(sub_id)},
        headers={"X-CSRF-Token": "wrong"},
    )

    assert r.status_code == 403
    assert fake.calls == []


async def test_requires_a_session(client):
    r = await client.post(
        "/me/push/test", json={"subscription_id": "00000000-0000-0000-0000-000000000000"}
    )
    assert r.status_code in (401, 403)
