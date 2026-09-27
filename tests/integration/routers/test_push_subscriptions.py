"""/me/push/subscriptions — store, list and forget Web Push subscriptions (NEU-1485)."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update

from tests.fixtures.users import _client_for
from tvbf.app.models import PushDelivery, PushSubscription
from tvbf.app.repos import push_delivery_repo
from tvbf.config import get_settings
from tvbf.main import app

ENDPOINT = "https://fcm.googleapis.com/fcm/send/abc123"
UA = "Mozilla/5.0 (Macintosh; Mac OS X 10_15_7) Safari/605"


def _body(endpoint: str = ENDPOINT, p256dh: str = "BKey", auth: str = "secret") -> dict:
    # `PushSubscription.toJSON()`'s shape, `expirationTime` included.
    return {"endpoint": endpoint, "expirationTime": None, "keys": {"p256dh": p256dh, "auth": auth}}


@pytest.fixture(autouse=True)
def vapid(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "vapid_private_key", "priv")
    monkeypatch.setattr(settings, "vapid_public_key", "BPubKey")
    monkeypatch.setattr(settings, "vapid_subject", "mailto:ops@example.com")
    return settings


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        yield c


@pytest.fixture
async def other_client(session, make_user):
    user = await make_user(email="other@example.com", verified=True)
    async for c in _client_for(session, user):
        yield c


async def _subscribe(client, **kwargs) -> UUID:
    r = await client.post(
        "/me/push/subscriptions", json=_body(**kwargs), headers={"User-Agent": UA}
    )
    assert r.status_code == 201, r.text
    return UUID(r.json()["id"])


async def _rows(session) -> list[PushSubscription]:
    result = await session.execute(
        select(PushSubscription).execution_options(populate_existing=True)
    )
    return list(result.scalars().all())


# --- POST -------------------------------------------------------------------


async def test_subscribe_stores_the_keys_and_user_agent(authed_client, session):
    sub_id = await _subscribe(authed_client)

    [row] = await _rows(session)
    assert row.id == sub_id
    assert row.user_id == authed_client.user.id
    assert (row.endpoint, row.p256dh, row.auth) == (ENDPOINT, "BKey", "secret")
    assert row.user_agent == UA
    assert row.failure_count == 0


async def test_subscribe_requires_csrf(authed_client):
    r = await authed_client.post(
        "/me/push/subscriptions", json=_body(), headers={"X-CSRF-Token": "wrong"}
    )
    assert r.status_code == 403


async def test_subscribe_requires_a_session(client):
    r = await client.post("/me/push/subscriptions", json=_body())
    assert r.status_code in (401, 403)


async def test_subscribe_is_503_when_vapid_is_not_configured(
    authed_client, vapid, monkeypatch, session
):
    monkeypatch.setattr(vapid, "vapid_private_key", None)
    r = await authed_client.post("/me/push/subscriptions", json=_body())
    assert r.status_code == 503
    assert r.json() == {"detail": "vapid_not_configured"}
    assert await _rows(session) == []


@pytest.mark.parametrize(
    "body",
    [
        _body(endpoint="http://fcm.googleapis.com/fcm/send/abc"),
        _body(endpoint="file:///etc/passwd"),
        _body(p256dh=""),
        {"endpoint": ENDPOINT},
    ],
)
async def test_subscribe_rejects_a_malformed_subscription(authed_client, body):
    r = await authed_client.post("/me/push/subscriptions", json=body)
    assert r.status_code == 422


async def test_resubscribing_the_same_endpoint_updates_the_one_row(authed_client, session):
    first = await _subscribe(authed_client)
    await session.execute(update(PushSubscription).values(failure_count=3))
    await session.commit()

    second = await _subscribe(authed_client, p256dh="BNewKey", auth="newsecret")

    assert second == first
    [row] = await _rows(session)
    assert (row.p256dh, row.auth, row.failure_count) == ("BNewKey", "newsecret", 0)


async def test_resubscribing_keeps_created_at_for_the_same_user(authed_client, session):
    await _subscribe(authed_client)
    long_ago = datetime.now(UTC) - timedelta(days=30)
    await session.execute(
        update(PushSubscription).values(created_at=long_ago, last_success_at=long_ago)
    )
    await session.commit()

    await _subscribe(authed_client)

    [row] = await _rows(session)
    assert row.created_at == long_ago
    assert row.last_success_at == long_ago


async def test_an_endpoint_moves_onto_the_user_who_subscribes_with_it(
    authed_client, other_client, session
):
    theirs = await _subscribe(authed_client)
    await push_delivery_repo.insert_pending(
        session,
        subscription_id=theirs,
        user_id=authed_client.user.id,
        notification_key="airs_today:7:2026-09-26",
        kind="airs_today",
    )
    await session.commit()

    mine = await _subscribe(other_client)

    # A fresh row, so the previous owner's history does not follow the device.
    assert mine != theirs
    [row] = await _rows(session)
    assert (row.id, row.user_id, row.last_success_at) == (mine, other_client.user.id, None)
    assert (await authed_client.get("/me/push/subscriptions")).json() == []
    assert [s["id"] for s in (await other_client.get("/me/push/subscriptions")).json()] == [
        str(mine)
    ]
    # The previous owner's log survives, still theirs, and does not block the
    # new owner from the same notification.
    [logged] = (
        await session.execute(select(PushDelivery).execution_options(populate_existing=True))
    ).scalars()
    assert (logged.subscription_id, logged.user_id) == (None, authed_client.user.id)
    assert (
        await push_delivery_repo.insert_pending(
            session,
            subscription_id=mine,
            user_id=other_client.user.id,
            notification_key="airs_today:7:2026-09-26",
            kind="airs_today",
        )
        is not None
    )


# --- GET --------------------------------------------------------------------


async def test_list_returns_only_mine_newest_first_and_never_the_keys(
    authed_client, other_client, session
):
    older = await _subscribe(authed_client, endpoint=f"{ENDPOINT}-old")
    newer = await _subscribe(authed_client)
    await _subscribe(other_client, endpoint=f"{ENDPOINT}-theirs")
    await session.execute(
        update(PushSubscription)
        .where(PushSubscription.endpoint == f"{ENDPOINT}-old")
        .values(created_at=datetime.now(UTC) - timedelta(days=1))
    )
    await session.commit()

    r = await authed_client.get("/me/push/subscriptions")

    assert r.status_code == 200
    rows = r.json()
    assert [row["id"] for row in rows] == [str(newer), str(older)]
    assert set(rows[0]) == {"id", "user_agent", "created_at", "last_success_at"}
    assert rows[0]["user_agent"] == UA
    assert rows[0]["last_success_at"] is None


async def test_list_requires_a_session(client):
    r = await client.get("/me/push/subscriptions")
    assert r.status_code == 401


# --- DELETE one -------------------------------------------------------------


async def test_delete_one_removes_it(authed_client, session):
    sub_id = await _subscribe(authed_client)

    r = await authed_client.delete(f"/me/push/subscriptions/{sub_id}")

    assert r.status_code == 204
    assert await _rows(session) == []


async def test_delete_one_is_404_for_someone_elses(authed_client, other_client, session):
    theirs = await _subscribe(other_client)

    r = await authed_client.delete(f"/me/push/subscriptions/{theirs}")

    assert r.status_code == 404
    assert len(await _rows(session)) == 1


async def test_delete_one_is_404_for_an_unknown_id(authed_client):
    r = await authed_client.delete("/me/push/subscriptions/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404


async def test_delete_one_requires_csrf(authed_client, session):
    sub_id = await _subscribe(authed_client)
    r = await authed_client.delete(
        f"/me/push/subscriptions/{sub_id}", headers={"X-CSRF-Token": "wrong"}
    )
    assert r.status_code == 403
    assert len(await _rows(session)) == 1


# --- DELETE all -------------------------------------------------------------


async def test_delete_all_removes_only_mine(authed_client, other_client, session):
    await _subscribe(authed_client)
    await _subscribe(authed_client, endpoint=f"{ENDPOINT}-2")
    await _subscribe(other_client, endpoint=f"{ENDPOINT}-theirs")

    r = await authed_client.delete("/me/push/subscriptions")

    assert r.status_code == 204
    [left] = await _rows(session)
    assert left.user_id == other_client.user.id


async def test_delete_all_is_204_with_nothing_to_delete(authed_client):
    r = await authed_client.delete("/me/push/subscriptions")
    assert r.status_code == 204


async def test_delete_all_requires_csrf(authed_client, session):
    await _subscribe(authed_client)
    r = await authed_client.delete("/me/push/subscriptions", headers={"X-CSRF-Token": "wrong"})
    assert r.status_code == 403
    assert len(await _rows(session)) == 1


# --- Delivery rows outlive their subscription (§4.3) -------------------------


async def test_deleting_a_subscription_nulls_its_delivery_rows(authed_client, session):
    sub_id = await _subscribe(authed_client)
    user_id = authed_client.user.id
    for key in ("airs_today:1:2026-09-26", "test:abc"):
        await push_delivery_repo.insert_pending(
            session, subscription_id=sub_id, user_id=user_id, notification_key=key, kind="test"
        )
    await session.commit()

    r = await authed_client.delete(f"/me/push/subscriptions/{sub_id}")
    assert r.status_code == 204

    result = await session.execute(select(PushDelivery).execution_options(populate_existing=True))
    rows = list(result.scalars().all())
    assert len(rows) == 2
    assert all(row.subscription_id is None for row in rows)
    assert all(row.user_id == user_id for row in rows)


async def test_insert_pending_claims_a_pair_once(authed_client, session):
    sub_id = await _subscribe(authed_client)

    async def claim() -> int | None:
        return await push_delivery_repo.insert_pending(
            session,
            subscription_id=sub_id,
            user_id=authed_client.user.id,
            notification_key="ended:42",
            kind="ended",
        )

    first = await claim()
    second = await claim()
    await session.commit()

    assert first is not None
    assert second is None
    [row] = (await session.execute(select(PushDelivery))).scalars().all()
    assert row.status == "pending"


async def test_deleting_the_account_takes_subscriptions_and_deliveries(authed_client, session):
    sub_id = await _subscribe(authed_client)
    await push_delivery_repo.insert_pending(
        session,
        subscription_id=sub_id,
        user_id=authed_client.user.id,
        notification_key="test:1",
        kind="test",
    )
    await session.commit()

    await session.delete(authed_client.user)
    await session.commit()

    assert await _rows(session) == []
    assert (await session.execute(select(PushDelivery))).scalars().all() == []
