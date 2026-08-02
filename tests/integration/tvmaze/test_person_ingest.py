"""Pass C — the person initial ingest (NEU-942).

One request per person: `/people/{id}?embed[]=guestcastcredits`. The todo list
is every id in `/updates/people` whose `credits_synced_at IS NULL`, so people
pass A created from a show's cast embed are picked up here for their credits.
"""

from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from tvbf.tvmaze import models as m
from tvbf.tvmaze import person_ingest as person_ingest_module
from tvbf.tvmaze.person_ingest import run_person_ingest


def person_payload(person_id: int, *, credits=None, updated: int = 1700000000) -> dict:
    return {
        "id": person_id,
        "name": f"Person {person_id}",
        "country": {"name": "United States", "code": "US", "timezone": "America/New_York"},
        "birthday": "",
        "deathday": "",
        "gender": "Male",
        "image": None,
        "updated": updated,
        "_embedded": {"guestcastcredits": credits or []},
    }


def guest_credit(episode_id: int, character_id: int, *, name: str = "Guest") -> dict:
    return {
        "self": False,
        "voice": False,
        "_links": {
            "episode": {"href": f"https://api.tvmaze.com/episodes/{episode_id}"},
            "character": {
                "href": f"https://api.tvmaze.com/characters/{character_id}",
                "name": name,
            },
        },
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


@pytest.fixture
async def episodes(session):
    """Guest credits need real episodes to point their FK at."""
    session.add(m.Show(id=1, name="S", tvmaze_updated=1))
    await session.flush()
    session.add_all(
        [
            m.Episode(id=500, show_id=1, season=1, number=1, name="E1"),
            m.Episode(id=501, show_id=1, season=1, number=2, name="E2"),
        ]
    )
    await session.commit()


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


async def _guest_rows(session, person_id: int) -> list[m.EpisodeGuestCast]:
    return list(
        (
            await session.execute(
                select(m.EpisodeGuestCast)
                .where(m.EpisodeGuestCast.person_id == person_id)
                .order_by(m.EpisodeGuestCast.sort_order)
            )
        )
        .scalars()
        .all()
    )


async def test_ingest_writes_person_and_guest_credits(session, run_id, episodes):
    client = FakeClient(
        {10: 100},
        {10: person_payload(10, credits=[guest_credit(500, 900), guest_credit(501, 901)])},
    )
    result = await run_person_ingest(session_factory=lambda: session, client=client, run_id=run_id)

    assert result.persons_processed == 1
    assert result.persons_failed == 0

    person = await _refreshed_person(session, 10)
    assert person.name == "Person 10"
    assert person.country_code == "US"
    assert person.birthday is None  # "" coerced, not a parse error
    assert person.credits_synced_at is not None

    rows = await _guest_rows(session, 10)
    assert [(r.episode_id, r.character_id) for r in rows] == [(500, 900), (501, 901)]


async def test_a_person_created_by_pass_a_is_picked_up_for_credits(session, run_id, episodes):
    """Pass A writes person rows from the cast embed but never their credits,
    so `credits_synced_at IS NULL` is what puts them in this todo list."""
    session.add(m.Person(id=11, name="From pass A", tvmaze_updated=1))
    await session.commit()

    client = FakeClient({11: 100}, {11: person_payload(11, credits=[guest_credit(500, 902)])})
    result = await run_person_ingest(session_factory=lambda: session, client=client, run_id=run_id)

    assert client.person_calls == [11]
    assert result.persons_processed == 1
    assert [r.episode_id for r in await _guest_rows(session, 11)] == [500]


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


async def test_guest_credits_are_replaced_not_appended_on_re_fetch(session, run_id, episodes):
    client = FakeClient({15: 100}, {15: person_payload(15, credits=[guest_credit(500, 903)])})
    await run_person_ingest(session_factory=lambda: session, client=client, run_id=run_id)
    assert len(await _guest_rows(session, 15)) == 1

    # Clear the watermark so the second run re-fetches, and change the credits.
    person = await _refreshed_person(session, 15)
    person.credits_synced_at = None
    await session.commit()

    client._people[15] = person_payload(15, credits=[guest_credit(501, 904)])
    second = m.IngestRun(id=uuid4(), kind="person_initial", status="running")
    session.add(second)
    await session.commit()
    await run_person_ingest(session_factory=lambda: session, client=client, run_id=second.id)

    rows = await _guest_rows(session, 15)
    assert [(r.episode_id, r.character_id) for r in rows] == [(501, 904)]


async def test_a_credit_for_an_unmirrored_episode_fails_that_person_only(session, run_id, episodes):
    """The FK doing its job: pass A is what fetches specials, and ~6% of
    guest-credited episodes are specials. The person stays unsynced so a later
    run retries them once pass A has landed."""
    client = FakeClient(
        {16: 100, 17: 100},
        {
            16: person_payload(16, credits=[guest_credit(999999, 905)]),
            17: person_payload(17, credits=[guest_credit(500, 906)]),
        },
    )
    result = await run_person_ingest(session_factory=lambda: session, client=client, run_id=run_id)

    assert result.persons_failed == 1
    assert result.persons_processed == 1
    assert (await _refreshed_person(session, 17)).credits_synced_at is not None
    assert (
        await session.execute(select(m.Person).where(m.Person.id == 16))
    ).scalar_one_or_none() is None


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

    await run_person_ingest(
        session_factory=lambda: session,
        client=FailingClient({60: 1700000000}),
        run_id=run_id,
        failure_threshold=1,
    )

    run = await _refreshed_run(session, run_id)
    assert run.status == "failed"
    assert run.last_update_cursor is None
