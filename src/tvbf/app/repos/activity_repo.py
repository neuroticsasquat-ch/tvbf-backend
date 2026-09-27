"""Friend-scoped reads over the activity ledger: the feed (NEU-178) and the
Popular with Friends ranking (NEU-1498).

The feed builds a reverse-chronological page of activity events from a set of
friend actors, applying read-time rollup of consecutive same-actor / same-show
`watched_episode` rows within a configurable time window into a single
`watched_episode_run` virtual item. Output is a list of `FeedRow` dataclasses;
hydration of user/show/episode display fields happens in the service layer.

The ranking lives beside it because it is the same table under the same two
sharing switches and the same show-resolution rule (ADR-0015). The two are two
copies rather than one shared fragment; each file's tests assert the switch
behaviour, so a predicate changed in one and not the other fails loudly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass
class FeedRow:
    item_id: str  # stable cursor key (singleton id or rollup min-id prefixed)
    sort_id: UUID  # tie-breaker for cursor (min id within rollup, or singleton id)
    actor_id: UUID
    kind: str  # FeedKind literal at the service layer
    show_id: int | None
    episode_id: int | None
    season_number: int | None
    rollup_count: int | None
    stars: float | None
    occurred_at: datetime


_QUERY = text(
    """
WITH joined AS (
    SELECT
        e.id,
        e.actor_id,
        e.verb,
        e.target_type,
        e.target_id,
        e.season_number,
        e.payload,
        e.created_at,
        CASE
            WHEN e.verb = 'watched_episode' THEN ep.show_id
            WHEN e.verb IN ('added_show','watched_season','watched_show','rated_show')
                THEN e.target_id::int
            WHEN e.verb = 'rated_episode' THEN ep.show_id
        END AS resolved_show_id
    FROM app.activity_event e
    JOIN app.user u ON u.id = e.actor_id AND u.activity_feed_enabled = TRUE
    LEFT JOIN catalog.episode ep
        ON e.target_type = 'episode' AND ep.id = e.target_id
    LEFT JOIN app.user_show_watch usw
        ON usw.user_id = e.actor_id
       AND usw.show_id = CASE
            WHEN e.verb = 'watched_episode' THEN ep.show_id
            WHEN e.verb IN ('added_show','watched_season','watched_show','rated_show')
                THEN e.target_id::int
            WHEN e.verb = 'rated_episode' THEN ep.show_id
        END
    WHERE e.actor_id = ANY(:friend_ids)
      AND (usw.hide_from_activity IS NOT TRUE)
),
gapped AS (
    SELECT
        *,
        CASE
            WHEN verb = 'watched_episode'
                 AND LAG(created_at) OVER (
                     PARTITION BY actor_id, resolved_show_id
                     ORDER BY created_at, id
                 ) > created_at - make_interval(mins => :window_min)
            THEN 0 ELSE 1
        END AS is_new_group
    FROM joined
),
grouped AS (
    SELECT
        *,
        SUM(is_new_group) OVER (
            PARTITION BY actor_id, resolved_show_id
            ORDER BY created_at, id
        ) AS grp
    FROM gapped
    WHERE verb = 'watched_episode'
),
collapsed AS (
    SELECT
        actor_id,
        resolved_show_id AS show_id,
        grp,
        COUNT(*) AS cnt,
        MAX(created_at) AS occurred_at,
        (array_agg(id ORDER BY created_at, id::text))[1] AS sort_id
    FROM grouped
    GROUP BY actor_id, resolved_show_id, grp
),
rollup_items AS (
    SELECT
        'r:' || sort_id::text AS item_id,
        sort_id,
        actor_id,
        'watched_episode_run' AS kind,
        show_id,
        NULL::int AS episode_id,
        NULL::int AS season_number,
        cnt::int AS rollup_count,
        NULL::float AS stars,
        occurred_at
    FROM collapsed
    WHERE cnt > 1
),
singleton_watched_episode AS (
    SELECT
        g.id::text AS item_id,
        g.id AS sort_id,
        g.actor_id,
        'watched_episode' AS kind,
        g.resolved_show_id AS show_id,
        g.target_id::int AS episode_id,
        NULL::int AS season_number,
        NULL::int AS rollup_count,
        NULL::float AS stars,
        g.created_at AS occurred_at
    FROM grouped g
    JOIN collapsed c
        ON c.actor_id = g.actor_id
           AND c.show_id IS NOT DISTINCT FROM g.resolved_show_id
           AND c.grp = g.grp
    WHERE c.cnt = 1
),
other_items AS (
    SELECT
        j.id::text AS item_id,
        j.id AS sort_id,
        j.actor_id,
        j.verb AS kind,
        CASE WHEN j.verb = 'rated_episode'
             THEN j.resolved_show_id ELSE j.target_id::int END AS show_id,
        CASE WHEN j.verb = 'rated_episode' THEN j.target_id::int ELSE NULL END AS episode_id,
        j.season_number,
        NULL::int AS rollup_count,
        CASE WHEN j.verb IN ('rated_show','rated_episode')
             THEN (j.payload->>'stars')::float ELSE NULL END AS stars,
        j.created_at AS occurred_at
    FROM joined j
    WHERE j.verb IN ('added_show','watched_season','watched_show','rated_show','rated_episode')
),
all_items AS (
    SELECT * FROM rollup_items
    UNION ALL SELECT * FROM singleton_watched_episode
    UNION ALL SELECT * FROM other_items
)
SELECT *
FROM all_items
WHERE (:has_cursor = FALSE)
   OR (occurred_at, sort_id) < (:cursor_ts, :cursor_id)
ORDER BY occurred_at DESC, sort_id DESC
LIMIT :limit
"""
).bindparams(bindparam("friend_ids", type_=ARRAY(PGUUID(as_uuid=True))))


async def fetch_feed_page(
    session: AsyncSession,
    *,
    friend_ids: list[UUID],
    window_min: int,
    limit: int,
    cursor_ts: datetime | None,
    cursor_id: UUID | None,
) -> list[FeedRow]:
    if not friend_ids:
        return []
    has_cursor = cursor_ts is not None and cursor_id is not None
    result = await session.execute(
        _QUERY,
        {
            "friend_ids": friend_ids,
            "window_min": window_min,
            "limit": limit,
            "has_cursor": has_cursor,
            "cursor_ts": cursor_ts,
            "cursor_id": cursor_id,
        },
    )
    rows: list[FeedRow] = []
    for r in result.mappings().all():
        rows.append(
            FeedRow(
                item_id=r["item_id"],
                sort_id=r["sort_id"],
                actor_id=r["actor_id"],
                kind=r["kind"],
                show_id=r["show_id"],
                episode_id=r["episode_id"],
                season_number=r["season_number"],
                rollup_count=r["rollup_count"],
                stars=r["stars"],
                occurred_at=r["occurred_at"],
            )
        )
    return rows


POPULAR_WINDOW_DAYS = 14
"""Project spec Q3: catches a weekly show twice and survives a quiet week."""

POPULAR_LIMIT = 24
"""Project spec Q9: matches Most Anticipated's grid."""


@dataclass(frozen=True)
class PopularShowRow:
    show_id: int
    friend_count: int


# The two sharing switches and the episode -> show resolution are the feed
# query's (`_QUERY` above). If those change, this changes with them. The verb
# allow-list is the feed's too: its CASE resolves an unknown verb to NULL, so a
# verb the feed would not show is not counted here either.
#
# The activity count and last-activity time are ordering keys only — they are
# never selected, so nothing downstream can expose them (project spec §5.2). The
# show id closes the ordering, so an unchanged ledger reads back identically.
#
# `adult` / `deleted_upstream_at` are filtered at read time, as every browse
# list does (`browse_queries.list_similar_shows`, `get_trending_snapshot`).
_POPULAR_QUERY = text(
    """
WITH resolved AS (
    SELECT
        e.actor_id,
        e.created_at,
        CASE WHEN e.target_type = 'episode' THEN ep.show_id
             ELSE e.target_id::int END AS show_id
    FROM app.activity_event e
    JOIN app.user u ON u.id = e.actor_id AND u.activity_feed_enabled = TRUE
    LEFT JOIN catalog.episode ep
        ON e.target_type = 'episode' AND ep.id = e.target_id
    WHERE e.actor_id = ANY(:friend_ids)
      AND e.verb IN ('added_show','watched_episode','watched_season',
                     'watched_show','rated_show','rated_episode')
      AND e.created_at >= now() - make_interval(days => :window_days)
),
visible AS (
    SELECT r.*
    FROM resolved r
    LEFT JOIN app.user_show_watch usw
        ON usw.user_id = r.actor_id AND usw.show_id = r.show_id
    WHERE r.show_id IS NOT NULL
      AND usw.hide_from_activity IS NOT TRUE
)
SELECT v.show_id, COUNT(DISTINCT v.actor_id) AS friend_count
FROM visible v
JOIN catalog.show s ON s.id = v.show_id
WHERE s.adult IS FALSE
  AND s.deleted_upstream_at IS NULL
GROUP BY v.show_id
ORDER BY COUNT(DISTINCT v.actor_id) DESC,
         COUNT(*) DESC,
         MAX(v.created_at) DESC,
         v.show_id
LIMIT :limit
"""
).bindparams(bindparam("friend_ids", type_=ARRAY(PGUUID(as_uuid=True))))


async def popular_shows_for_friends(
    session: AsyncSession,
    *,
    friend_ids: list[UUID],
    window_days: int = POPULAR_WINDOW_DAYS,
    limit: int = POPULAR_LIMIT,
) -> list[PopularShowRow]:
    """Shows ranked by how many of `friend_ids` were visibly active on them
    within the window (project spec §5.1).

    Order: distinct friends, then activity count, then most recent activity,
    then show id — total, so two reads of an unchanged ledger agree.

    `friend_ids` is expected to come from `connection_service.accepted_friend_ids`,
    which already drops pending, blocked and disabled users and never contains
    the viewer. Empty short-circuits without a query.
    """
    if not friend_ids:
        return []
    result = await session.execute(
        _POPULAR_QUERY,
        {"friend_ids": friend_ids, "window_days": window_days, "limit": limit},
    )
    return [
        PopularShowRow(show_id=r["show_id"], friend_count=r["friend_count"])
        for r in result.mappings().all()
    ]
