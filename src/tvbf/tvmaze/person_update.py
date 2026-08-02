import logging
from typing import Any
from uuid import UUID

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

    This is what keeps pass C's 75 hours from rotting. Person attributes change
    independently of any show — a performer's name change moves no show record —
    so without this job the mirror carries the old name indefinitely, because
    nothing would ever re-fetch that person.

    Cast and crew *membership* needs no help here: TV Maze cascades credit
    create/delete into `/updates/shows`, so the existing show delta keeps it
    current at zero extra request cost. This job covers what that misses.

    The watermark is read across `PERSON_CURSOR_KINDS`, never a bare kind: the
    initial person ingest finalizes with a cursor that the first delta inherits,
    exactly as `initial` hands off to `update` on the show axis. Scoping to
    `person_update` alone would make that first delta fall back to 0 and re-walk
    all 487k people.
    """
    async with _owned_session(session_factory) as s:
        cursor = await get_last_successful_cursor(s, kinds=PERSON_CURSOR_KINDS) or 0

    updates = await client.get_person_updates()
    todo = sorted(pid for pid, epoch in updates.items() if epoch > cursor)
    max_epoch = max((updates[pid] for pid in todo), default=cursor)

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
