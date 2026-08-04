import logging
from uuid import UUID

import httpx

from tvbf.tvmaze.api_payloads import TVMazeAka, TVMazeSeasonEpisode, TVMazeShow
from tvbf.tvmaze.client import TVMazeClient
from tvbf.tvmaze.ingest import IngestResult, SessionFactory, _fetch_episodes, _owned_session
from tvbf.tvmaze.runs import (
    SHOW_CURSOR_KINDS,
    finalize_run,
    get_last_successful_cursor,
    record_progress,
)
from tvbf.tvmaze.upsert import (
    clear_season_credits_synced,
    mark_akas_synced,
    upsert_akas,
    upsert_season_credits,
    upsert_show_payload,
)

log = logging.getLogger(__name__)


async def _refresh_season_credits(
    *,
    client: TVMazeClient,
    session_factory: SessionFactory,
    show: TVMazeShow,
) -> None:
    """Refetch each season of a just-written show for its episode credits.

    Measured at 307 shows/day × 2.11 seasons ≈ 650 requests, about 6 minutes —
    which is why the workflow's timeout had to move with this (ADR-0003).

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
    """
    for season in show.embedded.seasons:
        try:
            payload = await client.get_season_episodes(season.id)
            episodes = [TVMazeSeasonEpisode.model_validate(e) for e in payload]
            async with _owned_session(session_factory) as s:
                await upsert_season_credits(s, season_id=season.id, episodes=episodes)
                await s.commit()
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


async def run_update(
    *,
    session_factory: SessionFactory,
    client: TVMazeClient,
    run_id: UUID,
    failure_threshold: int = 10,
) -> IngestResult:
    async with _owned_session(session_factory) as s:
        cursor = await get_last_successful_cursor(s, kinds=SHOW_CURSOR_KINDS) or 0

    updates = await client.get_show_updates()
    todo = sorted(sid for sid, epoch in updates.items() if epoch > cursor)
    max_epoch = max((updates[sid] for sid in todo), default=cursor)

    processed = 0
    failed = 0
    consecutive_failures = 0

    for show_id in todo:
        try:
            payload = await client.get_show(show_id)
        except httpx.HTTPStatusError as e:
            log.warning("skipping show %d after http error: %s", show_id, e)
            failed += 1
            consecutive_failures += 1
            async with _owned_session(session_factory) as s:
                await record_progress(s, run_id, failed_delta=1)
                await s.commit()
            if consecutive_failures >= failure_threshold:
                async with _owned_session(session_factory) as s:
                    await finalize_run(
                        s,
                        run_id,
                        status="failed",
                        error=f"aborted after {consecutive_failures} consecutive failures",
                    )
                    await s.commit()
                return IngestResult(processed, failed, cursor)
            continue

        episodes = await _fetch_episodes(client, show_id)

        try:
            akas_payload = await client.get_akas(show_id)
        except Exception as e:
            log.warning(
                "akas fetch failed for show %d; will retry via backfill: %s",
                show_id,
                e,
            )
            akas_payload = None

        try:
            async with _owned_session(session_factory) as s:
                show = TVMazeShow.model_validate(payload)
                await upsert_show_payload(s, show, episodes=episodes)
                if akas_payload is not None:
                    akas = [TVMazeAka.model_validate(a) for a in akas_payload]
                    await upsert_akas(s, show_id=show.id, akas=akas)
                    await mark_akas_synced(s, show_id=show.id)
                await record_progress(s, run_id, processed_delta=1)
                await s.commit()
            processed += 1
            consecutive_failures = 0
        except Exception as e:
            log.exception("upsert failed for show %d", show_id)
            failed += 1
            consecutive_failures += 1
            async with _owned_session(session_factory) as s:
                await record_progress(s, run_id, failed_delta=1)
                await s.commit()
            if consecutive_failures >= failure_threshold:
                async with _owned_session(session_factory) as s:
                    await finalize_run(
                        s,
                        run_id,
                        status="failed",
                        error=f"aborted after {consecutive_failures} consecutive failures: {e}",
                    )
                    await s.commit()
                return IngestResult(processed, failed, cursor)
            continue

        # Season credits go last, and outside the show's transaction: they FK to
        # episode.id, so the episode rows written above must be committed first.
        await _refresh_season_credits(client=client, session_factory=session_factory, show=show)

    async with _owned_session(session_factory) as s:
        await finalize_run(s, run_id, status="succeeded", last_update_cursor=max_epoch)
        await s.commit()

    return IngestResult(processed, failed, max_epoch)
