import logging
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.tvmaze import models as m
from tvbf.tvmaze.api_payloads import TVMazePerson
from tvbf.tvmaze.client import is_gone_upstream
from tvbf.tvmaze.runs import (
    PERSON_CURSOR_KINDS,
    finalize_run,
    get_last_successful_cursor,
    record_progress,
    warn_if_all_gone,
)
from tvbf.tvmaze.upsert import upsert_persons

log = logging.getLogger(__name__)

SessionFactory = Callable[[], AsyncSession]


@dataclass
class PersonUpdateResult:
    persons_processed: int
    persons_failed: int
    last_update_cursor: int | None


@asynccontextmanager
async def _owned_session(session_factory: SessionFactory) -> AsyncIterator[AsyncSession]:
    """Yield a session via the factory's async context manager."""
    async with session_factory() as s:
        yield s


async def run_person_update(
    *,
    session_factory: SessionFactory,
    client: Any,  # duck-typed: needs `get_person_updates()` + `get_person(id)`
    run_id: UUID,
    failure_threshold: int = 10,
) -> PersonUpdateResult:
    """The daily person delta: `update.py`, pointed at the person axis.

    The only job left on the person axis. Purely an attribute refresh since
    ADR-0003 — it writes no credits, and NEU-962 retired the initial pass that
    used to precede it, because show cast/crew and season guest cast/crew embeds
    carry person objects byte-identical to `/people/{id}`.

    That does not make this optional. Person attributes change independently of
    any show: a performer's rename, a new headshot, a newly-added deathday mark
    no show updated, so without this job the mirror carries the old values
    indefinitely because nothing would ever re-fetch that person.

    Cast and crew *membership* needs no help here: TV Maze cascades credit
    create/delete into `/updates/shows`, so the show delta keeps it current at
    zero extra request cost, and the season fetch it triggers writes the
    episode-level credits.

    The watermark is read across `PERSON_CURSOR_KINDS` rather than a bare kind,
    so the person axis never resumes from the show axis's position — both write
    TV Maze epochs into the same column.
    """
    async with _owned_session(session_factory) as s:
        cursor = await get_last_successful_cursor(s, kinds=PERSON_CURSOR_KINDS) or 0
        held = set((await s.execute(select(m.Person.id))).scalars().all())

    updates = await client.get_person_updates()
    moved = {pid: epoch for pid, epoch in updates.items() if epoch > cursor}
    # The watermark covers everything considered, not just everything fetched.
    # Taking it over `todo` instead would peg it to the highest-epoch person we
    # happen to hold and re-consider the same strangers every day.
    max_epoch = max(moved.values(), default=cursor)
    # Scoped to people we already hold. Unscoped, the list is all 486,790
    # upstream people, and the ones we have no credit for would accrete as
    # zero-credit strangers whose pages render an empty filmography. Anyone who
    # later earns a credit arrives complete from the show or season embed, which
    # carries the same ten fields as `/people/{id}`.
    todo = sorted(pid for pid in moved if pid in held)

    return await process_people(
        session_factory=session_factory,
        client=client,
        run_id=run_id,
        todo=todo,
        failure_threshold=failure_threshold,
        cursor_on_success=max_epoch,
        # An aborted run finalizes without a cursor, so the watermark stays
        # where the last succeeded run left it and the skipped people are
        # picked up by the next delta.
        cursor_on_abort=cursor,
    )


async def process_people(
    *,
    session_factory: SessionFactory,
    client: Any,  # duck-typed: needs `get_person(id)`
    run_id: UUID,
    todo: Sequence[int],
    failure_threshold: int,
    cursor_on_success: int | None,
    cursor_on_abort: int | None,
) -> PersonUpdateResult:
    """Fetch and write each person in `todo`, one transaction apiece.

    Separate from `run_person_update` because the two answer different
    questions: that one decides *which* people are due, this one walks a work
    list and reports what happened to it. Kept parameterised over the cursor for
    the same reason — publishing the watermark is the caller's decision, and the
    abort path has to publish a different one from the success path.
    """
    processed = 0
    failed = 0
    consecutive_failures = 0
    gone = 0

    async def _record_failure(detail: str | None = None, *, is_gone: bool = False) -> bool:
        """Count one failed person; True if the run aborted on the threshold.

        Pass A open-codes this three times over; the cost of a divergence
        between the three arms is a re-run of the whole pass, so it lives in
        one place here.

        `is_gone` marks a person deleted upstream (a 404). It still counts
        toward `failed`, so the reported totals are unchanged, but it leaves the
        consecutive counter alone — the threshold means "upstream is broken",
        and a deleted person is not that (NEU-1006).
        """
        nonlocal failed, consecutive_failures, gone
        failed += 1
        if is_gone:
            gone += 1
        else:
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
            log.warning("person update: skipping person %d after http error: %s", person_id, e)
            if await _record_failure(is_gone=is_gone_upstream(e)):
                return PersonUpdateResult(processed, failed, cursor_on_abort)
            continue
        except Exception as e:
            log.exception("person update: unexpected error for person %d", person_id)
            if await _record_failure(str(e)):
                return PersonUpdateResult(processed, failed, cursor_on_abort)
            continue

        try:
            async with _owned_session(session_factory) as s:
                person = TVMazePerson.model_validate(payload)
                await upsert_persons(s, [person])
                await record_progress(s, run_id, processed_delta=1)
                await s.commit()
            processed += 1
            consecutive_failures = 0
        except Exception as e:
            log.exception("person update: write failed for person %d", person_id)
            if await _record_failure(str(e)):
                return PersonUpdateResult(processed, failed, cursor_on_abort)

    async with _owned_session(session_factory) as s:
        warn_if_all_gone(log, processed=processed, failed=failed, gone=gone, noun="people")
        await finalize_run(s, run_id, status="succeeded", last_update_cursor=cursor_on_success)
        await s.commit()

    return PersonUpdateResult(processed, failed, cursor_on_success)
