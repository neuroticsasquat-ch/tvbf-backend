"""One season's episode credits: fetch it, write it, and the show-level loop.

Three callers share this: the daily (`update.py`), pass A's show refresh
(`show_refresh.py`) and the historical backfill
(`episode_credits_backfill.py`). They agree on the fetch-and-write primitive
and disagree on what a failure means, which is why `write_season_credits`
raises and only `refresh_show_season_credits` swallows.
"""

import logging
from typing import Any

from tvbf.tvmaze.api_payloads import TVMazeSeasonEpisode, TVMazeShow
from tvbf.tvmaze.ingest import SessionFactory, _owned_session
from tvbf.tvmaze.upsert import clear_season_credits_synced, upsert_season_credits

log = logging.getLogger(__name__)


async def write_season_credits(
    *,
    client: Any,  # duck-typed: needs `async get_season_episodes(season_id) -> list[dict]`
    session_factory: SessionFactory,
    season_id: int,
) -> None:
    """Fetch one season's episodes with both credit embeds and write them.

    Owns its own transaction and raises on any failure — the caller decides
    what a failed season costs. The watermark is stamped inside
    `upsert_season_credits`, so a rollback leaves `credits_synced_at` NULL and
    the season stays in the backfill's todo list.
    """
    payload = await client.get_season_episodes(season_id)
    episodes = [TVMazeSeasonEpisode.model_validate(e) for e in payload]
    async with _owned_session(session_factory) as s:
        await upsert_season_credits(s, season_id=season_id, episodes=episodes)
        await s.commit()


async def refresh_show_season_credits(
    *,
    client: Any,  # duck-typed: needs `async get_season_episodes(season_id) -> list[dict]`
    session_factory: SessionFactory,
    show: TVMazeShow,
) -> None:
    """Refetch every season of a just-written show for its episode credits.

    Measured at 307 shows/day × 2.11 seasons ≈ 650 requests, about 6 minutes —
    which is why the daily workflow's timeout had to move with this (ADR-0003).

    Per-season failures are logged and swallowed rather than failing the show.
    The show itself is already committed and correct, and failing it instead
    would be actively wrong on the daily, whose cursor advances past failed
    shows and so never retries them.

    Handing the season back to the backfill means clearing its watermark, not
    just leaving it alone: a season failing its *first* refresh keeps a NULL
    `credits_synced_at` and is picked up for free, but one failing a later
    refresh would otherwise keep the stamp from its last success, and nothing —
    not the backfill, which selects on NULL, nor the daily, which has moved
    past this show — would ever come back for it.

    The caller must have committed the show's episode rows first: credits FK to
    `episode.id`.
    """
    for season in show.embedded.seasons:
        try:
            await write_season_credits(
                client=client, session_factory=session_factory, season_id=season.id
            )
        except Exception:
            log.exception(
                "season credits failed for show %d season %d; clearing its watermark "
                "so the backfill retries it",
                show.id,
                season.id,
            )
            try:
                async with _owned_session(session_factory) as s:
                    await clear_season_credits_synced(s, season_id=season.id)
                    await s.commit()
            except Exception:
                # Nothing left to fall back on — say so rather than let a second
                # failure escape and take the whole run down.
                log.exception(
                    "could not clear the credits watermark for season %d; it will read "
                    "as synced until something else refreshes it",
                    season.id,
                )
