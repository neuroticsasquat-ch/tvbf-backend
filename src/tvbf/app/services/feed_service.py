"""Feed assembly service (NEU-178).

Fetches a paginated feed page via `activity_repo`, then hydrates
display fields (actor display_name, show name, episode meta) in batch.
"""

from __future__ import annotations

import base64
import binascii
from datetime import datetime
from uuid import UUID

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.errors import InvalidCursor
from tvbf.app.repos import activity_repo, user_repo
from tvbf.app.repos.activity_repo import FeedRow
from tvbf.app.schemas import EpisodeMini, FeedItem, FeedPage, ShowMini, UserBrief
from tvbf.app.services import connection_service
from tvbf.catalog import seasons as season_rules
from tvbf.catalog.episodes import public_number
from tvbf.catalog.models import Episode, Season, Show
from tvbf.config import get_settings


def encode_cursor(occurred_at: datetime, sort_id: UUID) -> str:
    raw = f"{occurred_at.isoformat()}|{sort_id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode()).decode()
        ts_str, id_str = raw.split("|", 1)
        return datetime.fromisoformat(ts_str), UUID(id_str)
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise InvalidCursor("invalid_cursor") from exc


async def list_feed(
    db: AsyncSession, *, viewer_id: UUID, cursor: str | None, limit: int
) -> FeedPage:
    settings = get_settings()
    cursor_ts: datetime | None = None
    cursor_id: UUID | None = None
    if cursor is not None:
        cursor_ts, cursor_id = decode_cursor(cursor)

    friend_ids = list(await connection_service.accepted_friend_ids(db, viewer_id))
    rows = await activity_repo.fetch_feed_page(
        db,
        friend_ids=friend_ids,
        window_min=settings.activity_rollup_window_min,
        limit=limit,
        cursor_ts=cursor_ts,
        cursor_id=cursor_id,
    )

    items = await _hydrate(db, rows)
    next_cursor = None
    if len(rows) == limit and rows:
        last = rows[-1]
        next_cursor = encode_cursor(last.occurred_at, last.sort_id)
    return FeedPage(items=items, next_cursor=next_cursor)


async def _hydrate(db: AsyncSession, rows: list[FeedRow]) -> list[FeedItem]:
    if not rows:
        return []

    actor_ids: set[UUID] = {r.actor_id for r in rows}
    show_ids: set[int] = {r.show_id for r in rows if r.show_id is not None}
    episode_ids: set[int] = {r.episode_id for r in rows if r.episode_id is not None}

    season_keys: set[tuple[int, int]] = {
        (r.show_id, r.season_number)
        for r in rows
        if r.show_id is not None and r.season_number is not None
    }

    users = await user_repo.get_many_by_ids(db, actor_ids)
    shows = await _shows_by_id(db, show_ids)
    episodes = await _episodes_by_id(db, episode_ids)
    season_names = await _season_names_by_key(db, season_keys)

    items: list[FeedItem] = []
    for r in rows:
        actor_user = users.get(r.actor_id)
        if actor_user is None:
            continue
        show_obj = shows.get(r.show_id) if r.show_id is not None else None
        episode_obj = episodes.get(r.episode_id) if r.episode_id is not None else None
        items.append(
            FeedItem(
                id=r.item_id,
                actor=UserBrief(id=actor_user.id, display_name=actor_user.display_name),
                kind=r.kind,  # type: ignore[arg-type]
                show=(ShowMini(id=show_obj.id, name=show_obj.name) if show_obj else None),
                episode=(
                    EpisodeMini(
                        id=episode_obj.id,
                        name=episode_obj.name,
                        season=episode_obj.season_number,
                        number=public_number(episode_obj) or 0,
                    )
                    if episode_obj
                    else None
                ),
                season_number=r.season_number,
                season_name=(
                    season_names.get((r.show_id, r.season_number))
                    if r.show_id is not None and r.season_number is not None
                    else None
                ),
                rollup_count=r.rollup_count,
                stars=r.stars,
                occurred_at=r.occurred_at,
            )
        )
    return items


async def _shows_by_id(db: AsyncSession, ids: set[int]) -> dict[int, Show]:
    if not ids:
        return {}
    rows = (await db.execute(select(Show).where(Show.id.in_(ids)))).scalars().all()
    return {s.id: s for s in rows}


async def _episodes_by_id(db: AsyncSession, ids: set[int]) -> dict[int, Episode]:
    if not ids:
        return {}
    rows = (await db.execute(select(Episode).where(Episode.id.in_(ids)))).scalars().all()
    return {e.id: e for e in rows}


async def _season_names_by_key(
    db: AsyncSession, keys: set[tuple[int, int]]
) -> dict[tuple[int, int], str | None]:
    """Season names for the `(show_id, season_number)` pairs a feed page names.

    One batch query for the whole page, the same shape as `_shows_by_id` and
    `_episodes_by_id` — a per-item lookup would make the feed's query count scale
    with the page size.

    The pairs are matched as tuples rather than by widening to
    `show_id IN (...) AND season_number IN (...)`, which would drag in every
    other show's row at those numbers and make the dedupe below decide between
    rows no item asked about.

    **`catalog.season` deliberately carries no `UNIQUE (show_id, season_number)`**
    (NEU-1119 kept 18,339 copied seasons TMDB has no counterpart for, and 33
    pairs are still doubled outright), so a pair can return two rows in whichever
    order Postgres likes. `season_rules.deduped` is the one decision about which
    of them a read path shows — the same function `browse_queries.get_show_seasons`
    and `season_repo.unaired_for_shows` call. A missing pair and a season upstream
    never named both map to `None`, because the SPA falls back to the number for
    each.
    """
    if not keys:
        return {}
    rows = (
        (
            await db.execute(
                select(Season).where(tuple_(Season.show_id, Season.season_number).in_(list(keys)))
            )
        )
        .scalars()
        .all()
    )
    return {(s.show_id, s.season_number): s.name for s in season_rules.deduped(rows)}
