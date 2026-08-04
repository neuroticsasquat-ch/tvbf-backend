import logging
from typing import Any
from uuid import UUID

from sqlalchemy import select

from tvbf.tvmaze import models as m
from tvbf.tvmaze.person_ingest import (
    PersonIngestResult,
    SessionFactory,
    _owned_session,
    process_people,
)
from tvbf.tvmaze.runs import PERSON_CURSOR_KINDS, get_last_successful_cursor

log = logging.getLogger(__name__)


async def run_person_update(
    *,
    session_factory: SessionFactory,
    client: Any,  # duck-typed: needs `get_person_updates()` + `get_person(id)`
    run_id: UUID,
    failure_threshold: int = 10,
) -> PersonIngestResult:
    """The daily person delta: `update.py`, pointed at the person axis.

    Purely an attribute refresh since ADR-0003 — it writes no credits. That does
    not make it optional. Person attributes change independently of any show: a
    performer's rename, a new headshot, a newly-added deathday mark no show
    updated, so without this job the mirror carries the old values indefinitely
    because nothing would ever re-fetch that person.

    Cast and crew *membership* needs no help here: TV Maze cascades credit
    create/delete into `/updates/shows`, so the show delta keeps it current at
    zero extra request cost, and the season fetch it triggers writes the
    episode-level credits.

    The watermark is read across `PERSON_CURSOR_KINDS`, never a bare kind: the
    initial person ingest finalizes with a cursor that the first delta inherits,
    exactly as `initial` hands off to `update` on the show axis. Scoping to
    `person_update` alone would make that first delta fall back to 0 and re-walk
    all 487k people.
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
    # Scoped to people we already hold. Without the initial pass seeding the
    # table, an unscoped list is all 486,790 upstream people, and the ones we
    # have no credit for would accrete as zero-credit strangers whose pages
    # render an empty filmography. Anyone who later earns a credit arrives
    # complete from the show or season embed, which carries the same ten fields
    # as `/people/{id}`.
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
