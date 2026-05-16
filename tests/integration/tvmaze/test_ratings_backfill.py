from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from tvbf.tvmaze import models as m
from tvbf.tvmaze.ratings_backfill import run_ratings_backfill


class FakeClient:
    def __init__(self, payloads: dict[int, dict]):
        self._payloads = payloads
        self.calls: list[tuple[int, tuple[str, ...]]] = []

    async def get_show(self, show_id: int, *, embed: list[str] | None = None) -> dict:
        self.calls.append((show_id, tuple(embed or ())))
        return self._payloads[show_id]


class FailingClient:
    async def get_show(self, show_id: int, *, embed: list[str] | None = None) -> dict:
        raise httpx.HTTPStatusError(
            "boom",
            request=httpx.Request("GET", f"https://api.tvmaze.com/shows/{show_id}"),
            response=httpx.Response(500),
        )


def _show_payload(
    show_id: int, name: str, rating: float, episodes: list[dict] | None = None
) -> dict:
    return {
        "id": show_id,
        "name": name,
        "updated": 1,
        "rating": {"average": rating},
        "_embedded": {"episodes": episodes or [], "seasons": []},
    }


@pytest.fixture
async def two_unsynced_shows(session):
    session.add_all(
        [
            m.Show(id=1001, name="A", tvmaze_updated=1),
            m.Show(id=1002, name="B", tvmaze_updated=1),
        ]
    )
    await session.commit()


async def test_backfill_processes_unsynced_shows(session, two_unsynced_shows):
    run = m.IngestRun(id=uuid4(), kind="ratings_backfill", status="running")
    session.add(run)
    await session.commit()

    client = FakeClient(
        {
            1001: _show_payload(1001, "A", 7.0),
            1002: _show_payload(1002, "B", 8.0),
        }
    )
    result = await run_ratings_backfill(
        session_factory=lambda: session, client=client, run_id=run.id
    )
    assert sorted(c[0] for c in client.calls) == [1001, 1002]
    # Every call requested the episodes embed.
    assert all("episodes" in c[1] for c in client.calls)
    assert result.shows_processed == 2
    assert result.shows_failed == 0

    rows = (
        (
            await session.execute(
                select(m.Show)
                .where(m.Show.id.in_([1001, 1002]))
                .order_by(m.Show.id)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    assert [float(r.rating_average) for r in rows] == [7.0, 8.0]
    assert all(r.ratings_synced_at is not None for r in rows)

    refreshed = (
        await session.execute(
            select(m.IngestRun)
            .where(m.IngestRun.id == run.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert refreshed.status == "succeeded"


async def test_backfill_skips_already_synced(session):
    session.add_all(
        [
            m.Show(id=1010, name="Synced", tvmaze_updated=1, ratings_synced_at=datetime.now(UTC)),
            m.Show(id=1011, name="Unsynced", tvmaze_updated=1),
        ]
    )
    run = m.IngestRun(id=uuid4(), kind="ratings_backfill", status="running")
    session.add(run)
    await session.commit()

    client = FakeClient(
        {
            1010: _show_payload(1010, "Synced", 5.0),
            1011: _show_payload(1011, "Unsynced", 6.0),
        }
    )
    result = await run_ratings_backfill(
        session_factory=lambda: session, client=client, run_id=run.id
    )
    assert [c[0] for c in client.calls] == [1011]
    assert result.shows_processed == 1
    assert result.shows_failed == 0


async def test_backfill_aborts_after_consecutive_failure_threshold(session):
    # Seed 12 unsynced shows so default threshold of 10 will trip mid-run.
    session.add_all([m.Show(id=2000 + i, name=f"S{i}", tvmaze_updated=1) for i in range(12)])
    run = m.IngestRun(id=uuid4(), kind="ratings_backfill", status="running")
    session.add(run)
    await session.commit()

    calls: list[int] = []

    failing = FailingClient()

    class CountingFailingClient:
        async def get_show(self, show_id: int, *, embed: list[str] | None = None) -> dict:
            calls.append(show_id)
            return await failing.get_show(show_id, embed=embed)

    result = await run_ratings_backfill(
        session_factory=lambda: session,
        client=CountingFailingClient(),
        run_id=run.id,
    )
    assert result.shows_processed == 0
    assert result.shows_failed == 10
    # Aborted after exactly 10 failures — no 11th call.
    assert len(calls) == 10

    refreshed = (
        await session.execute(
            select(m.IngestRun)
            .where(m.IngestRun.id == run.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert refreshed.status == "failed"
    assert refreshed.error is not None
    assert "aborted after 10 consecutive failures" in refreshed.error


async def test_backfill_upserts_episode_ratings(session):
    session.add(m.Show(id=3001, name="WithEps", tvmaze_updated=1))
    await session.commit()

    run = m.IngestRun(id=uuid4(), kind="ratings_backfill", status="running")
    session.add(run)
    await session.commit()

    episodes = [
        {
            "id": 90001,
            "name": "Pilot",
            "season": 1,
            "number": 1,
            "type": "regular",
            "airdate": "",
            "airtime": "",
            "airstamp": None,
            "runtime": 60,
            "rating": {"average": 9.2},
        },
        {
            "id": 90002,
            "name": "Two",
            "season": 1,
            "number": 2,
            "type": "regular",
            "airdate": "",
            "airtime": "",
            "airstamp": None,
            "runtime": 60,
            "rating": {"average": 8.4},
        },
    ]
    client = FakeClient({3001: _show_payload(3001, "WithEps", 8.5, episodes=episodes)})

    result = await run_ratings_backfill(
        session_factory=lambda: session, client=client, run_id=run.id
    )
    assert result.shows_processed == 1
    assert result.shows_failed == 0

    rows = (
        (
            await session.execute(
                select(m.Episode)
                .where(m.Episode.id.in_([90001, 90002]))
                .order_by(m.Episode.id)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    assert [float(r.rating_average) for r in rows] == [9.2, 8.4]
