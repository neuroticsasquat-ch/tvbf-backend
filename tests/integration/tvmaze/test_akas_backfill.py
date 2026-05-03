from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from tvbf.tvmaze import models as m
from tvbf.tvmaze.akas_backfill import run_akas_backfill


class FakeClient:
    def __init__(self, payloads: dict[int, list[dict]]):
        self._payloads = payloads
        self.calls: list[int] = []

    async def get_akas(self, show_id: int) -> list[dict]:
        self.calls.append(show_id)
        return self._payloads.get(show_id, [])


class FailingClient:
    async def get_akas(self, show_id: int) -> list[dict]:
        raise httpx.HTTPStatusError(
            "boom",
            request=httpx.Request("GET", f"https://api.tvmaze.com/shows/{show_id}/akas"),
            response=httpx.Response(500),
        )


@pytest.fixture
async def two_unsynced_shows(session):
    session.add_all(
        [
            m.Show(id=10, name="Foo", tvmaze_updated=1),
            m.Show(id=11, name="Bar", tvmaze_updated=1),
        ]
    )
    await session.commit()


async def test_backfill_processes_unsynced_shows(session, two_unsynced_shows):
    run = m.IngestRun(id=uuid4(), kind="akas_backfill", status="running")
    session.add(run)
    await session.commit()

    client = FakeClient(
        {
            10: [
                {
                    "name": "Foo (US)",
                    "country": {"code": "US", "name": "United States"},
                    "language": "en",
                }
            ],
            11: [],
        }
    )
    result = await run_akas_backfill(session_factory=lambda: session, client=client, run_id=run.id)
    assert sorted(client.calls) == [10, 11]
    assert result.shows_processed == 2
    assert result.shows_failed == 0

    rows = (
        (
            await session.execute(
                select(m.Show)
                .where(m.Show.id.in_([10, 11]))
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    assert all(s.akas_synced_at is not None for s in rows)

    aka_rows = (await session.execute(select(m.ShowAka))).scalars().all()
    assert {r.show_id for r in aka_rows} == {10}


async def test_backfill_skips_already_synced(session):
    session.add_all(
        [
            m.Show(id=20, name="A", tvmaze_updated=1, akas_synced_at=datetime.now(UTC)),
            m.Show(id=21, name="B", tvmaze_updated=1),
        ]
    )
    run = m.IngestRun(id=uuid4(), kind="akas_backfill", status="running")
    session.add(run)
    await session.commit()

    client = FakeClient({20: [{"name": "X", "country": None}], 21: []})
    await run_akas_backfill(session_factory=lambda: session, client=client, run_id=run.id)
    assert client.calls == [21]


async def test_backfill_handles_per_show_failure(session):
    session.add_all([m.Show(id=30, name="A", tvmaze_updated=1)])
    run = m.IngestRun(id=uuid4(), kind="akas_backfill", status="running")
    session.add(run)
    await session.commit()

    result = await run_akas_backfill(
        session_factory=lambda: session, client=FailingClient(), run_id=run.id
    )
    assert result.shows_failed == 1
    assert result.shows_processed == 0


async def test_backfill_aborts_after_consecutive_failure_threshold(session):
    # Seed 3 unsynced shows, all of which will fail.
    session.add_all(
        [
            m.Show(id=40, name="A", tvmaze_updated=1),
            m.Show(id=41, name="B", tvmaze_updated=1),
            m.Show(id=42, name="C", tvmaze_updated=1),
        ]
    )
    run = m.IngestRun(id=uuid4(), kind="akas_backfill", status="running")
    session.add(run)
    await session.commit()

    failing = FailingClient()
    # Wrap to count calls so we can assert the abort prevents a third call.
    calls: list[int] = []

    class CountingFailingClient:
        async def get_akas(self, show_id: int) -> list[dict]:
            calls.append(show_id)
            return await failing.get_akas(show_id)

    result = await run_akas_backfill(
        session_factory=lambda: session,
        client=CountingFailingClient(),
        run_id=run.id,
        failure_threshold=2,
    )
    assert result.shows_failed == 2
    assert result.shows_processed == 0
    # Aborted before processing the third show.
    assert calls == [40, 41]

    refreshed = (
        await session.execute(
            select(m.IngestRun)
            .where(m.IngestRun.id == run.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert refreshed.status == "failed"
    assert refreshed.error is not None
