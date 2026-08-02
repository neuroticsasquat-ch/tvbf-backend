import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.tvmaze import models as m
from tvbf.tvmaze.api_payloads import TVMazePersonDetail
from tvbf.tvmaze.runs import finalize_run, record_progress
from tvbf.tvmaze.upsert import (
    mark_person_credits_synced,
    upsert_person_guest_cast,
    upsert_persons,
)

log = logging.getLogger(__name__)

SessionFactory = Callable[[], AsyncSession]


@dataclass
class PersonIngestResult:
    persons_processed: int
    persons_failed: int
    last_update_cursor: int | None


@asynccontextmanager
async def _owned_session(session_factory: SessionFactory) -> AsyncIterator[AsyncSession]:
    """Yield a session via the factory's async context manager."""
    async with session_factory() as s:
        yield s


async def run_person_ingest(
    *,
    session_factory: SessionFactory,
    client: Any,  # duck-typed: needs `get_person_updates()` + `get_person(id)`
    run_id: UUID,
    failure_threshold: int = 10,
) -> PersonIngestResult:
    """Pass C: mirror every person, and their guest-cast credits as a byproduct.

    One request per person — `/people/{id}?embed[]=guestcastcredits` — across
    ~487k people. Guest credits are unreachable from the show side at any
    acceptable cost (`/episodes/{id}?embed[]=guestcast` is the only route and
    that is 3.4M requests), and every guest credit involves exactly one person,
    so walking all people covers all guest credits by definition.

    The todo list is every id in `/updates/people` whose `credits_synced_at IS
    NULL`, which correctly picks up the people pass A created as a side effect
    of the show cast/crew embeds but never fetched credits for. Each person
    runs in its own transaction, so a crash mid-run leaves earlier people
    synced and the next trigger resumes from the watermark.

    Per-person failures bump `shows_failed` and abort the run after
    `failure_threshold` consecutive failures, mirroring pass A. A credit
    pointing at an episode we don't mirror raises on the FK and fails that
    person — deliberately, since it means pass A's specials never landed and
    the run should be stopped rather than silently dropping credits.
    """
    updates = await client.get_person_updates()
    # Captured before the loop, exactly as the show ingest does: without it the
    # first person delta has no cursor to inherit and re-walks all 487k people.
    cursor = max(updates.values()) if updates else None

    async with _owned_session(session_factory) as s:
        synced = set(
            (await s.execute(select(m.Person.id).where(m.Person.credits_synced_at.is_not(None))))
            .scalars()
            .all()
        )
    todo = sorted(pid for pid in updates if pid not in synced)

    processed = 0
    failed = 0
    consecutive_failures = 0

    async def _record_failure(detail: str | None = None) -> bool:
        """Count one failed person; True if the run aborted on the threshold.

        Pass A open-codes this three times over; at 487k people the cost of a
        divergence between the three arms is a 75-hour re-run, so it lives in
        one place here.
        """
        nonlocal failed, consecutive_failures
        failed += 1
        consecutive_failures += 1
        async with _owned_session(session_factory) as s:
            await record_progress(s, run_id, failed_delta=1)
            await s.commit()
        if consecutive_failures < failure_threshold:
            return False
        error = f"aborted after {consecutive_failures} consecutive failures"
        if detail is not None:
            error = f"{error}: {detail}"
        async with _owned_session(session_factory) as s:
            await finalize_run(s, run_id, status="failed", error=error)
            await s.commit()
        return True

    for person_id in todo:
        try:
            payload = await client.get_person(person_id)
        except httpx.HTTPStatusError as e:
            log.warning("person ingest: skipping person %d after http error: %s", person_id, e)
            if await _record_failure():
                return PersonIngestResult(processed, failed, cursor)
            continue
        except Exception as e:
            log.exception("person ingest: unexpected error for person %d", person_id)
            if await _record_failure(str(e)):
                return PersonIngestResult(processed, failed, cursor)
            continue

        try:
            async with _owned_session(session_factory) as s:
                person = TVMazePersonDetail.model_validate(payload)
                await upsert_persons(s, [person])
                await upsert_person_guest_cast(
                    s, person_id=person.id, credits=person.embedded.guestcastcredits
                )
                await mark_person_credits_synced(s, person_id=person.id)
                await record_progress(s, run_id, processed_delta=1)
                await s.commit()
            processed += 1
            consecutive_failures = 0
        except Exception as e:
            log.exception("person ingest: write failed for person %d", person_id)
            if await _record_failure(str(e)):
                return PersonIngestResult(processed, failed, cursor)

    async with _owned_session(session_factory) as s:
        await finalize_run(s, run_id, status="succeeded", last_update_cursor=cursor)
        await s.commit()

    return PersonIngestResult(processed, failed, cursor)
