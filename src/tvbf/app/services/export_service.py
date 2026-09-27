"""Streaming JSON export of a user's account-level data.

The document shape is locked by the ticket:

    {
      "account": { id, email, email_verified_at, display_name, handle, created_at },
      "my_shows":      [ { show_id, show_name, added_at }, ... ],
      "watch_history": [ { episode_id, show_id, season, number, watched_at }, ... ],
      "push_subscriptions": [ { id, user_agent, created_at, last_success_at }, ... ],
      "notification_preferences": { notify_airs_today, notify_premiere_set,
                                    notify_premiere_moved, notify_ended, notify_revived }
    }

The two push keys are NEU-1493's (push spec §7). A subscription is exported as
`GET /me/push/subscriptions` shows it — never the endpoint or the keys, which
are capability secrets rather than the user's data.

We stream rather than buffer the whole thing because watch history can grow
unboundedly. `stream_export` is an async generator over UTF-8 string chunks;
the route wraps it in a `StreamingResponse`.

Per-row encoding uses `json.dumps(default=_json_default)` so datetimes and
UUIDs serialize to ISO-8601 / string without us hand-formatting each field.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.models import User, UserEpisodeWatch, UserShowWatch
from tvbf.app.repos import push_subscription_repo
from tvbf.catalog.models import Episode, Show


def _json_default(obj: Any) -> Any:
    if isinstance(obj, datetime | date):
        return obj.isoformat()
    if isinstance(obj, UUID):
        return str(obj)
    raise TypeError(f"unserializable type: {type(obj).__name__}")


def _dumps(obj: Any) -> str:
    return json.dumps(obj, default=_json_default, separators=(",", ":"))


def _account_payload(user: User) -> dict[str, Any]:
    return {
        "id": user.id,
        "email": user.email,
        "email_verified_at": user.email_verified_at,
        "display_name": user.display_name,
        "handle": user.handle,
        "created_at": user.created_at,
    }


def _notification_preferences_payload(user: User) -> dict[str, Any]:
    return {
        "notify_airs_today": user.notify_airs_today,
        "notify_premiere_set": user.notify_premiere_set,
        "notify_premiere_moved": user.notify_premiere_moved,
        "notify_ended": user.notify_ended,
        "notify_revived": user.notify_revived,
    }


async def stream_export(db: AsyncSession, *, user: User) -> AsyncIterator[str]:
    """Yield the export document piece by piece."""
    yield '{"account":'
    yield _dumps(_account_payload(user))

    # my_shows
    yield ',"my_shows":['
    first = True
    my_shows_stmt = (
        select(Show.id, Show.name, UserShowWatch.created_at)
        .join(UserShowWatch, UserShowWatch.show_id == Show.id)
        .where(UserShowWatch.user_id == user.id)
        .order_by(UserShowWatch.created_at)
        .execution_options(yield_per=200)
    )
    result = await db.stream(my_shows_stmt)
    async for show_id, show_name, added_at in result:
        sep = "" if first else ","
        first = False
        yield sep + _dumps({"show_id": show_id, "show_name": show_name, "added_at": added_at})
    yield "]"

    # watch_history
    yield ',"watch_history":['
    first = True
    history_stmt = (
        select(
            UserEpisodeWatch.episode_id,
            Episode.show_id,
            Episode.season_number,
            Episode.episode_number,
            UserEpisodeWatch.watched_at,
        )
        .join(Episode, Episode.id == UserEpisodeWatch.episode_id)
        .where(UserEpisodeWatch.user_id == user.id)
        .order_by(UserEpisodeWatch.watched_at, UserEpisodeWatch.episode_id)
        .execution_options(yield_per=500)
    )
    result = await db.stream(history_stmt)
    async for episode_id, show_id, season, number, watched_at in result:
        sep = "" if first else ","
        first = False
        yield sep + _dumps(
            {
                "episode_id": episode_id,
                "show_id": show_id,
                "season": season,
                "number": number,
                "watched_at": watched_at,
            }
        )
    yield "]"

    # push_subscriptions — a handful per user at most, so no streaming.
    subscriptions = await push_subscription_repo.list_for_user(db, user.id)
    yield ',"push_subscriptions":'
    yield _dumps(
        [
            {
                "id": sub.id,
                "user_agent": sub.user_agent,
                "created_at": sub.created_at,
                "last_success_at": sub.last_success_at,
            }
            for sub in subscriptions
        ]
    )

    yield ',"notification_preferences":'
    yield _dumps(_notification_preferences_payload(user))
    yield "}"
