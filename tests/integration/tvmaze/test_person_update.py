"""The daily person delta — NEU-943.

`update.py`, pointed at the person axis: read the watermark, take everything in
`/updates/people` past it that we already hold, re-fetch attributes, advance the
cursor. What it exists for is the attribute change no show record reflects — a
performer's rename never touches a show, so nothing else would re-fetch them.
It writes no credits since ADR-0003, and since NEU-962 it is the only job on the
axis: the initial pass it used to inherit a cursor from is gone.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from tvbf.tvmaze import models as m
from tvbf.tvmaze import person_update as person_update_module
from tvbf.tvmaze.person_update import run_person_update
from tvbf.tvmaze.runs import PERSON_CURSOR_KINDS, get_last_successful_cursor


def person_payload(person_id: int, *, updated: int = 1700000000) -> dict:
    return {
        "id": person_id,
        "name": f"Person {person_id}",
        "country": {"name": "United States", "code": "US", "timezone": "America/New_York"},
        "birthday": "",
        "deathday": "",
        "gender": "Male",
        "image": None,
        "updated": updated,
    }


class FakeClient:
    """Duck-types the two calls the delta makes, recording what it was asked for."""

    def __init__(self, updates: dict[int, int], people: dict[int, dict] | None = None):
        self._updates = updates
        self._people = people or {}
        self.person_calls: list[int] = []

    async def get_person_updates(self) -> dict[int, int]:
        return dict(self._updates)

    async def get_person(self, person_id: int) -> dict:
        self.person_calls.append(person_id)
        return self._people[person_id]


def _http_error(person_id: int) -> httpx.HTTPStatusError:
    return httpx.HTTPStatusError(
        "boom",
        request=httpx.Request("GET", f"https://api.tvmaze.com/people/{person_id}"),
        response=httpx.Response(500),
    )


@pytest.fixture
async def run_id(session):
    run = m.IngestRun(id=uuid4(), kind="person_update", status="running")
    session.add(run)
    await session.commit()
    return run.id


async def _hold(session, *person_ids: int) -> None:
    """Mirror these people, so the delta's held-scoping lets them through."""
    session.add_all(
        [m.Person(id=pid, name=f"Person {pid}", tvmaze_updated=1) for pid in person_ids]
    )
    await session.commit()


async def _succeeded_run(session, *, kind: str, cursor: int, finished_at: datetime) -> None:
    session.add(
        m.IngestRun(
            id=uuid4(),
            kind=kind,
            status="succeeded",
            last_update_cursor=cursor,
            finished_at=finished_at,
        )
    )
    await session.commit()


async def _refreshed_run(session, rid) -> m.IngestRun:
    return (
        await session.execute(
            select(m.IngestRun)
            .where(m.IngestRun.id == rid)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def _refreshed_person(session, person_id: int) -> m.Person:
    return (
        await session.execute(
            select(m.Person)
            .where(m.Person.id == person_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def test_only_people_past_the_cursor_are_re_fetched(session, run_id):
    now = datetime.now(UTC)
    await _succeeded_run(session, kind="person_update", cursor=1700000000, finished_at=now)
    await _hold(session, 70, 71)

    client = FakeClient(
        {70: 1699999999, 71: 1700000001},
        {71: person_payload(71, updated=1700000001)},
    )
    result = await run_person_update(session_factory=lambda: session, client=client, run_id=run_id)

    assert client.person_calls == [71]  # 70 didn't move; 71 did
    assert result.persons_processed == 1
    assert result.last_update_cursor == 1700000001


async def test_a_newer_show_axis_cursor_is_not_borrowed(session, run_id):
    """Both axes write TV Maze epochs into the same column, so a show cursor
    read here would not error — it would just silently skip people."""
    now = datetime.now(UTC)
    await _succeeded_run(
        session, kind="person_update", cursor=1700000000, finished_at=now - timedelta(hours=1)
    )
    await _succeeded_run(session, kind="update", cursor=1700009999, finished_at=now)
    await _hold(session, 74)

    client = FakeClient({74: 1700000500}, {74: person_payload(74, updated=1700000500)})
    await run_person_update(session_factory=lambda: session, client=client, run_id=run_id)

    assert client.person_calls == [74]


async def test_first_ever_run_with_no_cursor_walks_everything(session, run_id):
    await _hold(session, 75, 76)
    client = FakeClient(
        {75: 1, 76: 2}, {75: person_payload(75, updated=1), 76: person_payload(76, updated=2)}
    )
    result = await run_person_update(session_factory=lambda: session, client=client, run_id=run_id)

    assert client.person_calls == [75, 76]
    assert result.persons_processed == 2
    assert result.last_update_cursor == 2


async def test_the_todo_list_comes_from_upstream_epochs_not_from_row_state(session, run_id):
    """The todo list is built from `/updates/people` epochs and nothing else.

    NEU-962 dropped `person.credits_synced_at`, which this used to demonstrate
    by seeding the watermark; the property it guarded is still real and still
    load-bearing. A person we have already mirrored in full is exactly the
    person whose name changes, so any per-row "already done" gate would make the
    rename this job exists for unreachable. The two people below are seeded
    identically — the only thing separating them is what upstream reports.
    """
    session.add_all(
        [
            m.Person(id=77, name="Old name", tvmaze_updated=1700000000),
            m.Person(id=78, name="Same vintage", tvmaze_updated=1700000000),
        ]
    )
    await session.commit()
    await _succeeded_run(
        session, kind="person_update", cursor=1700000000, finished_at=datetime.now(UTC)
    )

    payload = person_payload(77, updated=1700000500)
    payload["name"] = "New name"
    client = FakeClient({77: 1700000500, 78: 1699999999}, {77: payload})
    await run_person_update(session_factory=lambda: session, client=client, run_id=run_id)

    assert client.person_calls == [77]
    assert (await _refreshed_person(session, 77)).name == "New name"


async def test_people_we_do_not_hold_are_not_fetched(session, run_id):
    """Without an initial pass seeding the table, an unscoped todo list is all
    486,790 upstream people, and the ones with no credit anywhere would accrete
    as zero-credit strangers whose pages render an empty filmography."""
    await _hold(session, 78)

    client = FakeClient({78: 100, 79: 100}, {78: person_payload(78)})
    result = await run_person_update(session_factory=lambda: session, client=client, run_id=run_id)

    assert client.person_calls == [78]
    assert result.persons_processed == 1
    assert (
        await session.execute(select(m.Person).where(m.Person.id == 79))
    ).scalar_one_or_none() is None


async def test_the_cursor_covers_people_the_scoping_skipped(session, run_id):
    """The watermark records what was considered, not what was fetched. Taking
    it over the fetched list would peg it to the highest-epoch person we happen
    to hold and re-consider the same strangers every day."""
    await _hold(session, 78)

    client = FakeClient({78: 100, 79: 9999}, {78: person_payload(78)})
    result = await run_person_update(session_factory=lambda: session, client=client, run_id=run_id)

    assert client.person_calls == [78]
    assert result.last_update_cursor == 9999


async def test_the_daily_writes_no_credit_rows(session, run_id):
    """The ownership cutover (ADR-0003). A payload still carrying the embed we
    no longer request must not put a row in a credit table."""
    await _hold(session, 78)
    payload = person_payload(78)
    payload["_embedded"] = {
        "guestcastcredits": [
            {
                "_links": {
                    "episode": {"href": "https://api.tvmaze.com/episodes/500"},
                    "character": {"href": "https://api.tvmaze.com/characters/900"},
                }
            }
        ]
    }

    client = FakeClient({78: 100}, {78: payload})
    await run_person_update(session_factory=lambda: session, client=client, run_id=run_id)

    assert (await session.execute(select(m.EpisodeGuestCast))).scalars().all() == []


async def test_a_failed_write_leaves_no_partial_person_row(session, run_id, monkeypatch):
    """One transaction per person. Fails *after* the upsert, so only an actual
    rollback can keep the row out — a half-written person would otherwise be
    counted as failed on the run while sitting in the table."""
    await _hold(session, 85)
    real_record_progress = person_update_module.record_progress

    async def boom_on_success(s, rid, processed_delta=0, failed_delta=0):
        if processed_delta:
            raise RuntimeError("simulated failure after the upsert")
        return await real_record_progress(
            s, rid, processed_delta=processed_delta, failed_delta=failed_delta
        )

    monkeypatch.setattr(person_update_module, "record_progress", boom_on_success)

    payload = person_payload(85)
    payload["name"] = "Renamed"
    client = FakeClient({85: 1700000500}, {85: payload})
    result = await run_person_update(session_factory=lambda: session, client=client, run_id=run_id)

    assert result.persons_failed == 1
    assert result.persons_processed == 0
    assert (await _refreshed_person(session, 85)).name == "Person 85"  # not "Renamed"


async def test_the_cursor_advances_only_on_success(session, run_id):
    await _succeeded_run(
        session, kind="person_update", cursor=1700000000, finished_at=datetime.now(UTC)
    )
    await _hold(session, 79)

    class FailingClient(FakeClient):
        async def get_person(self, person_id: int) -> dict:
            raise _http_error(person_id)

    result = await run_person_update(
        session_factory=lambda: session,
        client=FailingClient({79: 1700009999}),
        run_id=run_id,
        failure_threshold=1,
    )

    run = await _refreshed_run(session, run_id)
    assert run.status == "failed"
    assert run.last_update_cursor is None
    # The watermark stays where the last succeeded run left it, so the next
    # delta retries the people this run never got through.
    assert result.last_update_cursor == 1700000000
    assert await get_last_successful_cursor(session, kinds=PERSON_CURSOR_KINDS) == 1700000000


async def test_a_run_with_nothing_to_do_republishes_the_cursor(session, run_id):
    """`max(..., default=cursor)` — an empty delta must not finalize with 0 and
    hand the next run a watermark of nothing."""
    await _succeeded_run(
        session, kind="person_update", cursor=1700000000, finished_at=datetime.now(UTC)
    )

    client = FakeClient({80: 1699999999})
    result = await run_person_update(session_factory=lambda: session, client=client, run_id=run_id)

    assert client.person_calls == []
    assert result.last_update_cursor == 1700000000
    assert (await _refreshed_run(session, run_id)).last_update_cursor == 1700000000


async def test_a_failure_is_non_fatal_and_counted_on_the_run(session, run_id):
    await _hold(session, 81, 82)

    class OneFailingClient(FakeClient):
        async def get_person(self, person_id: int) -> dict:
            if person_id == 81:
                raise _http_error(person_id)
            return await super().get_person(person_id)

    client = OneFailingClient({81: 100, 82: 100}, {82: person_payload(82)})
    result = await run_person_update(session_factory=lambda: session, client=client, run_id=run_id)

    assert result.persons_failed == 1
    assert result.persons_processed == 1
    run = await _refreshed_run(session, run_id)
    assert run.status == "succeeded"
    assert run.shows_failed == 1


async def test_the_watermark_advances_past_a_non_fatally_failed_person(session, run_id):
    """Inherited from `update.py`, and pinned here because it is a real gap:
    `max_epoch` is computed over the whole todo list, so a person who failed
    without tripping the abort threshold is behind the new watermark and isn't
    retried until upstream bumps them again. Pass C used to be the safety net —
    its todo list was watermark-driven rather than epoch-driven — and NEU-962
    retired it, so nothing catches these now. Accepted: the loss is one
    refreshed headshot or rename until upstream next touches that person, and
    the person axis is attributes only.
    """
    await _hold(session, 83, 84)

    class OneFailingClient(FakeClient):
        async def get_person(self, person_id: int) -> dict:
            if person_id == 83:
                raise _http_error(person_id)
            return await super().get_person(person_id)

    client = OneFailingClient({83: 1700000900, 84: 1700000100}, {84: person_payload(84)})
    result = await run_person_update(session_factory=lambda: session, client=client, run_id=run_id)

    assert result.persons_failed == 1
    assert result.last_update_cursor == 1700000900
    assert (await _refreshed_run(session, run_id)).last_update_cursor == 1700000900


async def test_aborts_after_consecutive_failure_threshold(session, run_id):
    await _hold(session, 90, 91, 92)
    calls: list[int] = []

    class CountingFailingClient(FakeClient):
        async def get_person(self, person_id: int) -> dict:
            calls.append(person_id)
            raise _http_error(person_id)

    result = await run_person_update(
        session_factory=lambda: session,
        client=CountingFailingClient({90: 1, 91: 1, 92: 1}),
        run_id=run_id,
        failure_threshold=2,
    )

    assert result.persons_failed == 2
    assert calls == [90, 91]  # aborted before the third
    run = await _refreshed_run(session, run_id)
    assert run.status == "failed"
    assert run.error is not None and "2 consecutive failures" in run.error
