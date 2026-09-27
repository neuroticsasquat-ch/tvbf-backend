"""GET /me/export's push half: subscriptions and notification preferences (NEU-1493, spec §7)."""

import json

from tvbf.app.models import PushSubscription


async def test_export_carries_subscriptions_without_keys_or_endpoint(
    authed_client, session, make_user
):
    me = authed_client.user
    other = await make_user(email="other@example.com")
    mine = PushSubscription(
        user_id=me.id,
        endpoint="https://push.example.com/mine",
        p256dh="secret-p256dh",
        auth="secret-auth",
        user_agent="Firefox",
    )
    session.add(mine)
    session.add(
        PushSubscription(
            user_id=other.id, endpoint="https://push.example.com/theirs", p256dh="k", auth="a"
        )
    )
    await session.commit()
    await session.refresh(mine)

    r = await authed_client.get("/me/export")
    assert r.status_code == 200
    assert "secret" not in r.text
    assert "push.example.com" not in r.text
    body = json.loads(r.text)
    assert body["push_subscriptions"] == [
        {
            "id": str(mine.id),
            "user_agent": "Firefox",
            "created_at": mine.created_at.isoformat(),
            "last_success_at": None,
        }
    ]


async def test_export_carries_notification_preferences(authed_client, session):
    me = authed_client.user
    me.notify_ended = False
    await session.commit()

    body = json.loads((await authed_client.get("/me/export")).text)
    assert body["notification_preferences"] == {
        "notify_airs_today": True,
        "notify_premiere_set": True,
        "notify_premiere_moved": True,
        "notify_ended": False,
        "notify_revived": True,
    }


async def test_export_with_no_subscriptions_is_an_empty_list(authed_client):
    body = json.loads((await authed_client.get("/me/export")).text)
    assert body["push_subscriptions"] == []
