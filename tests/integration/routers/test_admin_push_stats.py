"""GET /admin/push/stats — push delivery stats for the admin page (NEU-1493, spec §5.4)."""

from datetime import UTC, date, datetime, timedelta
from uuid import UUID

from httpx import ASGITransport, AsyncClient

from tvbf.app.models import PushDelivery, PushSubscription
from tvbf.main import app


async def _as_admin(client, session):
    me = client.user  # type: ignore[attr-defined]
    me.is_admin = True
    await session.commit()
    return me


async def _subscription(session, user, n: int) -> PushSubscription:
    row = PushSubscription(
        user_id=user.id, endpoint=f"https://push.example.com/{user.id}/{n}", p256dh="k", auth="a"
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def _delivery(
    session,
    *,
    user,
    key: str,
    status: str,
    created_at: datetime,
    subscription_id: UUID | None = None,
    error: str | None = None,
) -> None:
    session.add(
        PushDelivery(
            subscription_id=subscription_id,
            user_id=user.id,
            notification_key=key,
            kind="airs_today",
            status=status,
            error=error,
            created_at=created_at,
        )
    )
    await session.commit()


def _today() -> date:
    return datetime.now(UTC).date()


# --- the gate ----------------------------------------------------------------


async def test_requires_a_session():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        r = await c.get("/admin/push/stats")
    assert r.status_code == 401


async def test_forbidden_for_non_admin(authed_client):
    r = await authed_client.get("/admin/push/stats")
    assert r.status_code == 403
    assert r.json()["detail"] == "admin_required"


# --- the payload -------------------------------------------------------------


async def test_empty_tables_give_zeroes_and_thirty_zero_filled_days(authed_client, session):
    await _as_admin(authed_client, session)
    r = await authed_client.get("/admin/push/stats")
    assert r.status_code == 200
    assert r.headers["Cache-Control"] == "private, no-store"
    body = r.json()
    assert body["subscriptions"] == 0
    assert body["users_subscribed"] == 0
    days = [d["day"] for d in body["by_day"]]
    assert len(days) == 30
    assert days[-1] == _today().isoformat()
    assert days[0] == (_today() - timedelta(days=29)).isoformat()
    assert all(d["sent"] == d["failed"] == d["retired"] == 0 for d in body["by_day"])


async def test_counts_subscriptions_and_distinct_subscribed_users(
    authed_client, session, make_user
):
    me = await _as_admin(authed_client, session)
    other = await make_user(email="other@example.com")
    await _subscription(session, me, 1)
    await _subscription(session, me, 2)
    await _subscription(session, other, 1)

    body = (await authed_client.get("/admin/push/stats")).json()
    assert body["subscriptions"] == 3
    assert body["users_subscribed"] == 2


async def test_by_day_counts_sent_failed_and_retired(authed_client, session, make_user):
    me = await _as_admin(authed_client, session)
    other = await make_user(email="other@example.com")
    sub = await _subscription(session, other, 1)
    now = datetime.now(UTC)
    yesterday = now - timedelta(days=1)

    await _delivery(
        session, user=other, key="a", status="sent", created_at=now, subscription_id=sub.id
    )
    await _delivery(
        session, user=other, key="b", status="sent", created_at=now, subscription_id=sub.id
    )
    await _delivery(
        session, user=other, key="c", status="failed", created_at=now, subscription_id=sub.id
    )
    # Retirements: the subscription is gone, so the row's `subscription_id` is null.
    await _delivery(
        session, user=other, key="d", status="failed", created_at=yesterday, error="gone"
    )
    await _delivery(
        session, user=me, key="e", status="failed", created_at=yesterday, error="failure_limit"
    )
    # Neither sent nor failed: counted nowhere.
    await _delivery(
        session, user=other, key="f", status="pending", created_at=now, subscription_id=sub.id
    )
    await _delivery(
        session, user=other, key="g", status="skipped", created_at=now, subscription_id=sub.id
    )

    by_day = {d["day"]: d for d in (await authed_client.get("/admin/push/stats")).json()["by_day"]}
    assert by_day[now.date().isoformat()] == {
        "day": now.date().isoformat(),
        "sent": 2,
        "failed": 1,
        "retired": 0,
    }
    assert by_day[yesterday.date().isoformat()] == {
        "day": yesterday.date().isoformat(),
        "sent": 0,
        "failed": 2,
        "retired": 2,
    }


async def test_rows_older_than_thirty_days_are_left_out(authed_client, session):
    me = await _as_admin(authed_client, session)
    old = datetime.now(UTC) - timedelta(days=30)
    await _delivery(session, user=me, key="old", status="sent", created_at=old)

    body = (await authed_client.get("/admin/push/stats")).json()
    assert sum(d["sent"] for d in body["by_day"]) == 0
