"""The daily person delta — NEU-943.

`update.py`, pointed at the person axis: read the watermark, take everything in
`/updates/people` past it, re-fetch with `embed[]=guestcastcredits`, advance the
cursor. What it exists for is the attribute change no show record reflects — a
performer's rename never touches a show, so nothing else would re-fetch them.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from tvbf.tvmaze import models as m
from tvbf.tvmaze.person_update import run_person_update
from tvbf.tvmaze.runs import PERSON_CURSOR_KINDS, get_last_successful_cursor

from .test_person_ingest import FakeClient, guest_credit, person_payload


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


async def test_only_people_past_the_cursor_are_re_fetched(session, run_id):
    now = datetime.now(UTC)
    await _succeeded_run(session, kind="person_update", cursor=1700000000, finished_at=now)

    client = FakeClient(
        {70: 1699999999, 71: 1700000001},
        {71: person_payload(71, updated=1700000001)},
    )
    result = await run_person_update(session_factory=lambda: session, client=client, run_id=run_id)

    assert client.person_calls == [71]  # 70 didn't move; 71 did
    assert result.persons_processed == 1
    assert result.last_update_cursor == 1700000001


async def test_the_initial_ingests_cursor_is_inherited_by_the_first_delta(session, run_id):
    """`person_initial` and `person_update` share one lineage. Scoping the
    lookup to this kind alone would send the first delta back to 0 and re-walk
    all 487k people."""
    now = datetime.now(UTC)
    await _succeeded_run(session, kind="person_initial", cursor=1700000000, finished_at=now)

    client = FakeClient(
        {72: 1699999999, 73: 1700000500},
        {73: person_payload(73, updated=1700000500)},
    )
    await run_person_update(session_factory=lambda: session, client=client, run_id=run_id)

    assert client.person_calls == [73]


async def test_a_newer_show_axis_cursor_is_not_borrowed(session, run_id):
    """Both axes write TV Maze epochs into the same column, so a show cursor
    read here would not error — it would just silently skip people."""
    now = datetime.now(UTC)
    await _succeeded_run(
        session, kind="person_update", cursor=1700000000, finished_at=now - timedelta(hours=1)
    )
    await _succeeded_run(session, kind="update", cursor=1700009999, finished_at=now)

    client = FakeClient({74: 1700000500}, {74: person_payload(74, updated=1700000500)})
    await run_person_update(session_factory=lambda: session, client=client, run_id=run_id)

    assert client.person_calls == [74]


async def test_first_ever_run_with_no_cursor_walks_everything(session, run_id):
    client = FakeClient(
        {75: 1, 76: 2}, {75: person_payload(75, updated=1), 76: person_payload(76, updated=2)}
    )
    result = await run_person_update(session_factory=lambda: session, client=client, run_id=run_id)

    assert client.person_calls == [75, 76]
    assert result.persons_processed == 2
    assert result.last_update_cursor == 2


async def test_an_already_synced_person_is_re_fetched_when_their_epoch_moves(session, run_id):
    """The delta's todo list is epoch-driven, not `credits_synced_at`-driven —
    otherwise the rename this job exists for would never be picked up."""
    session.add(
        m.Person(
            id=77, name="Old name", tvmaze_updated=1700000000, credits_synced_at=datetime.now(UTC)
        )
    )
    await session.commit()
    await _succeeded_run(
        session, kind="person_update", cursor=1700000000, finished_at=datetime.now(UTC)
    )

    payload = person_payload(77, updated=1700000500)
    payload["name"] = "New name"
    client = FakeClient({77: 1700000500}, {77: payload})
    await run_person_update(session_factory=lambda: session, client=client, run_id=run_id)

    assert client.person_calls == [77]
    assert (await _refreshed_person(session, 77)).name == "New name"


async def test_guest_credits_are_replaced_not_appended(session, run_id, episodes):
    client = FakeClient({78: 100}, {78: person_payload(78, credits=[guest_credit(500, 910)])})
    await run_person_update(session_factory=lambda: session, client=client, run_id=run_id)
    assert [(r.episode_id, r.character_id) for r in await _guest_rows(session, 78)] == [(500, 910)]

    client._people[78] = person_payload(78, credits=[guest_credit(501, 911)], updated=200)
    client._updates = {78: 200}
    second = m.IngestRun(id=uuid4(), kind="person_update", status="running")
    session.add(second)
    await session.commit()
    await run_person_update(session_factory=lambda: session, client=client, run_id=second.id)

    assert [(r.episode_id, r.character_id) for r in await _guest_rows(session, 78)] == [(501, 911)]


async def test_the_cursor_advances_only_on_success(session, run_id):
    await _succeeded_run(
        session, kind="person_update", cursor=1700000000, finished_at=datetime.now(UTC)
    )

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


async def test_aborts_after_consecutive_failure_threshold(session, run_id):
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
