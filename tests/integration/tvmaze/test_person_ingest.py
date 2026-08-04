"""Pass C — the person initial ingest (NEU-942).

One request per person: `/people/{id}`, attributes only. It used to embed
`guestcastcredits` and write them; ADR-0003 moved every credit table to the
show axis, so what remains here is the person mirror and its
`credits_synced_at IS NULL` todo list.
"""

from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from tvbf.tvmaze import models as m
from tvbf.tvmaze import person_ingest as person_ingest_module
from tvbf.tvmaze.person_ingest import run_person_ingest


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
    """Duck-types the two calls pass C makes, recording what it was asked for."""

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
    run = m.IngestRun(id=uuid4(), kind="person_initial", status="running")
    session.add(run)
    await session.commit()
    return run.id


async def _refreshed_person(session, person_id: int) -> m.Person:
    return (
        await session.execute(
            select(m.Person)
            .where(m.Person.id == person_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def _refreshed_run(session, rid) -> m.IngestRun:
    return (
        await session.execute(
            select(m.IngestRun)
            .where(m.IngestRun.id == rid)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def test_ingest_writes_person_attributes(session, run_id):
    client = FakeClient({10: 100}, {10: person_payload(10)})
    result = await run_person_ingest(session_factory=lambda: session, client=client, run_id=run_id)

    assert result.persons_processed == 1
    assert result.persons_failed == 0

    person = await _refreshed_person(session, 10)
    assert person.name == "Person 10"
    assert person.country_code == "US"
    assert person.birthday is None  # "" coerced, not a parse error
    assert person.credits_synced_at is not None


async def test_the_person_pass_writes_no_credit_rows(session, run_id):
    """The ownership cutover (ADR-0003): every credit table is written by the
    show axis now, so this pass must not touch them even when upstream sends a
    credit embed we no longer ask for."""
    payload = person_payload(11)
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
    client = FakeClient({11: 100}, {11: payload})
    await run_person_ingest(session_factory=lambda: session, client=client, run_id=run_id)

    assert (await session.execute(select(m.EpisodeGuestCast))).scalars().all() == []


async def test_a_person_created_by_pass_a_is_picked_up(session, run_id):
    """Pass A writes person rows from the cast embed, so `credits_synced_at IS
    NULL` is what puts them in this todo list."""
    session.add(m.Person(id=12, name="From pass A", tvmaze_updated=1))
    await session.commit()

    client = FakeClient({12: 100}, {12: person_payload(12)})
    result = await run_person_ingest(session_factory=lambda: session, client=client, run_id=run_id)

    assert client.person_calls == [12]
    assert result.persons_processed == 1


async def test_already_synced_people_are_skipped(session, run_id):
    session.add(m.Person(id=12, name="Done", tvmaze_updated=1, credits_synced_at=datetime.now(UTC)))
    session.add(m.Person(id=13, name="Todo", tvmaze_updated=1))
    await session.commit()

    client = FakeClient({12: 100, 13: 100}, {13: person_payload(13)})
    result = await run_person_ingest(session_factory=lambda: session, client=client, run_id=run_id)

    assert client.person_calls == [13]
    assert result.persons_processed == 1


async def test_rerun_is_a_no_op(session, run_id):
    client = FakeClient({14: 100}, {14: person_payload(14)})
    await run_person_ingest(session_factory=lambda: session, client=client, run_id=run_id)
    assert client.person_calls == [14]

    second = m.IngestRun(id=uuid4(), kind="person_initial", status="running")
    session.add(second)
    await session.commit()
    result = await run_person_ingest(
        session_factory=lambda: session, client=client, run_id=second.id
    )

    assert client.person_calls == [14]  # watermark respected
    assert result.persons_processed == 0


async def test_http_failure_is_non_fatal_and_counted_on_the_run(session, run_id):
    class FailingClient(FakeClient):
        async def get_person(self, person_id: int) -> dict:
            raise _http_error(person_id)

    result = await run_person_ingest(
        session_factory=lambda: session, client=FailingClient({18: 100}), run_id=run_id
    )

    assert result.persons_failed == 1
    assert result.persons_processed == 0
    # Counted on the run row, not just in memory — a 75h run's only progress
    # signal is what the status endpoint reads back.
    assert (await _refreshed_run(session, run_id)).shows_failed == 1


async def test_watermark_rolls_back_when_the_write_fails_after_stamping(
    session, run_id, monkeypatch
):
    """The resumability guard: fail *after* `credits_synced_at` is stamped, so
    only an actual rollback can keep the person in the todo list."""
    real_record_progress = person_ingest_module.record_progress

    async def boom_on_success(s, rid, processed_delta=0, failed_delta=0):
        if processed_delta:
            raise RuntimeError("simulated failure after stamping")
        return await real_record_progress(
            s, rid, processed_delta=processed_delta, failed_delta=failed_delta
        )

    monkeypatch.setattr(person_ingest_module, "record_progress", boom_on_success)

    client = FakeClient({19: 100}, {19: person_payload(19)})
    result = await run_person_ingest(session_factory=lambda: session, client=client, run_id=run_id)

    assert result.persons_failed == 1
    assert result.persons_processed == 0
    assert (
        await session.execute(select(m.Person).where(m.Person.id == 19))
    ).scalar_one_or_none() is None


async def test_aborts_after_consecutive_failure_threshold(session, run_id):
    calls: list[int] = []

    class CountingFailingClient(FakeClient):
        async def get_person(self, person_id: int) -> dict:
            calls.append(person_id)
            raise _http_error(person_id)

    result = await run_person_ingest(
        session_factory=lambda: session,
        client=CountingFailingClient({40: 1, 41: 1, 42: 1}),
        run_id=run_id,
        failure_threshold=2,
    )

    assert result.persons_failed == 2
    assert calls == [40, 41]  # aborted before the third

    run = await _refreshed_run(session, run_id)
    assert run.status == "failed"
    assert run.error is not None and "2 consecutive failures" in run.error


async def test_successful_run_finalizes_with_the_cursor(session, run_id):
    """Without a cursor the first person delta has nothing to inherit and
    re-walks all 487k people."""
    client = FakeClient(
        {50: 1700000000, 51: 1700000500}, {50: person_payload(50), 51: person_payload(51)}
    )
    result = await run_person_ingest(session_factory=lambda: session, client=client, run_id=run_id)

    assert result.last_update_cursor == 1700000500
    run = await _refreshed_run(session, run_id)
    assert run.status == "succeeded"
    assert run.last_update_cursor == 1700000500


async def test_an_aborted_run_does_not_publish_a_cursor(session, run_id):
    """A failed run must not hand a cursor to the person delta — the delta
    would then skip every person this run never reached."""

    class FailingClient(FakeClient):
        async def get_person(self, person_id: int) -> dict:
            raise _http_error(person_id)

    result = await run_person_ingest(
        session_factory=lambda: session,
        client=FailingClient({60: 1700000000}),
        run_id=run_id,
        failure_threshold=1,
    )

    run = await _refreshed_run(session, run_id)
    assert run.status == "failed"
    assert run.last_update_cursor is None
    # The return value must agree with what was persisted.
    assert result.last_update_cursor is None
