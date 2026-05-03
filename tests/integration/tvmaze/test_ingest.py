import re

import httpx
import respx
from sqlalchemy import select

from tests.fixtures.tvmaze.show_factory import make_show
from tvbf.tvmaze import models as m
from tvbf.tvmaze.client import TVMazeClient
from tvbf.tvmaze.ingest import run_initial_ingest
from tvbf.tvmaze.runs import create_run

_AKAS_URL_RE = re.compile(r"https://api\.tvmaze\.com/shows/\d+/akas")


def _mock_akas_default_empty() -> None:
    """Default-mock /akas endpoints to return an empty list for any show id."""
    respx.get(url__regex=_AKAS_URL_RE).mock(return_value=httpx.Response(200, json=[]))


@respx.mock
async def test_initial_ingest_inserts_all_shows(session):
    respx.get("https://api.tvmaze.com/updates/shows").mock(
        return_value=httpx.Response(200, json={"1": 100, "2": 200})
    )
    respx.get("https://api.tvmaze.com/shows/1").mock(
        return_value=httpx.Response(200, json=make_show(1, 100))
    )
    respx.get("https://api.tvmaze.com/shows/2").mock(
        return_value=httpx.Response(200, json=make_show(2, 200))
    )

    _mock_akas_default_empty()
    run_id = await create_run(session, kind="initial")
    await session.commit()

    async with TVMazeClient(
        "https://api.tvmaze.com", rate_calls=50, rate_window=1, retry_base_delay=0.01
    ) as c:
        result = await run_initial_ingest(session_factory=lambda: session, client=c, run_id=run_id)

    assert result.shows_processed == 2
    assert result.shows_failed == 0
    assert result.last_update_cursor == 200
    shows = (await session.execute(select(m.Show))).scalars().all()
    assert {s.id for s in shows} == {1, 2}


@respx.mock
async def test_initial_ingest_skips_already_present_shows(session):
    session.add(m.Show(id=1, name="pre", tvmaze_updated=999))
    await session.commit()

    respx.get("https://api.tvmaze.com/updates/shows").mock(
        return_value=httpx.Response(200, json={"1": 100, "2": 200})
    )
    show2 = respx.get("https://api.tvmaze.com/shows/2").mock(
        return_value=httpx.Response(200, json=make_show(2, 200))
    )
    show1 = respx.get("https://api.tvmaze.com/shows/1").mock(
        return_value=httpx.Response(200, json=make_show(1, 100))
    )

    _mock_akas_default_empty()
    run_id = await create_run(session, kind="initial")
    await session.commit()

    async with TVMazeClient(
        "https://api.tvmaze.com", rate_calls=50, rate_window=1, retry_base_delay=0.01
    ) as c:
        result = await run_initial_ingest(session_factory=lambda: session, client=c, run_id=run_id)

    assert result.shows_processed == 1
    assert show2.call_count == 1
    assert show1.call_count == 0


@respx.mock
async def test_initial_ingest_continues_past_per_show_failures(session):
    respx.get("https://api.tvmaze.com/updates/shows").mock(
        return_value=httpx.Response(200, json={"1": 100, "2": 200, "3": 300})
    )
    respx.get("https://api.tvmaze.com/shows/1").mock(
        return_value=httpx.Response(200, json=make_show(1, 100))
    )
    respx.get("https://api.tvmaze.com/shows/2").mock(return_value=httpx.Response(404))
    respx.get("https://api.tvmaze.com/shows/3").mock(
        return_value=httpx.Response(200, json=make_show(3, 300))
    )

    _mock_akas_default_empty()
    run_id = await create_run(session, kind="initial")
    await session.commit()

    async with TVMazeClient(
        "https://api.tvmaze.com",
        rate_calls=50,
        rate_window=1,
        retry_max_attempts=2,
        retry_base_delay=0.01,
    ) as c:
        result = await run_initial_ingest(session_factory=lambda: session, client=c, run_id=run_id)

    assert result.shows_processed == 2
    assert result.shows_failed == 1
    shows = (await session.execute(select(m.Show))).scalars().all()
    assert {s.id for s in shows} == {1, 3}


@respx.mock
async def test_initial_ingest_aborts_after_consecutive_http_failures(session):
    respx.get("https://api.tvmaze.com/updates/shows").mock(
        return_value=httpx.Response(200, json={"1": 100, "2": 200, "3": 300})
    )
    respx.get("https://api.tvmaze.com/shows/1").mock(return_value=httpx.Response(404))
    respx.get("https://api.tvmaze.com/shows/2").mock(return_value=httpx.Response(404))
    respx.get("https://api.tvmaze.com/shows/3").mock(return_value=httpx.Response(404))

    _mock_akas_default_empty()
    run_id = await create_run(session, kind="initial")
    await session.commit()

    async with TVMazeClient(
        "https://api.tvmaze.com",
        rate_calls=50,
        rate_window=1,
        retry_max_attempts=1,
        retry_base_delay=0.001,
    ) as c:
        result = await run_initial_ingest(
            session_factory=lambda: session,
            client=c,
            run_id=run_id,
            failure_threshold=2,
        )

    assert result.shows_processed == 0
    assert result.shows_failed == 2
    row = (
        await session.execute(
            select(m.IngestRun).where(m.IngestRun.id == run_id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()
    assert row.status == "failed"
    assert row.error is not None
    assert "consecutive failures" in row.error


@respx.mock
async def test_initial_ingest_catches_non_http_errors_and_continues(session):
    respx.get("https://api.tvmaze.com/updates/shows").mock(
        return_value=httpx.Response(200, json={"1": 100, "2": 200})
    )
    respx.get("https://api.tvmaze.com/shows/1").mock(side_effect=httpx.ConnectError("boom"))
    respx.get("https://api.tvmaze.com/shows/2").mock(
        return_value=httpx.Response(200, json=make_show(2, 200))
    )

    _mock_akas_default_empty()
    run_id = await create_run(session, kind="initial")
    await session.commit()

    async with TVMazeClient(
        "https://api.tvmaze.com",
        rate_calls=50,
        rate_window=1,
        retry_max_attempts=1,
        retry_base_delay=0.001,
    ) as c:
        result = await run_initial_ingest(session_factory=lambda: session, client=c, run_id=run_id)

    assert result.shows_processed == 1
    assert result.shows_failed == 1


@respx.mock
async def test_initial_ingest_catches_upsert_errors_and_continues(session, monkeypatch):
    respx.get("https://api.tvmaze.com/updates/shows").mock(
        return_value=httpx.Response(200, json={"1": 100, "2": 200})
    )
    respx.get("https://api.tvmaze.com/shows/1").mock(
        return_value=httpx.Response(200, json=make_show(1, 100))
    )
    respx.get("https://api.tvmaze.com/shows/2").mock(
        return_value=httpx.Response(200, json=make_show(2, 200))
    )

    from tvbf.tvmaze import upsert as upsert_module

    real_upsert = upsert_module.upsert_show_payload
    call_count = {"n": 0}

    async def broken_then_real(s, show):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated upsert failure")
        return await real_upsert(s, show)

    monkeypatch.setattr("tvbf.tvmaze.ingest.upsert_show_payload", broken_then_real)

    _mock_akas_default_empty()
    run_id = await create_run(session, kind="initial")
    await session.commit()

    async with TVMazeClient(
        "https://api.tvmaze.com",
        rate_calls=50,
        rate_window=1,
        retry_max_attempts=1,
        retry_base_delay=0.001,
    ) as c:
        result = await run_initial_ingest(session_factory=lambda: session, client=c, run_id=run_id)

    assert result.shows_processed == 1
    assert result.shows_failed == 1


@respx.mock
async def test_initial_ingest_persists_akas_for_each_show(session):
    """When TVMaze returns AKA entries for a show, the ingest path stores them
    in tvmaze.show_aka and stamps tvmaze.show.akas_synced_at."""
    respx.get("https://api.tvmaze.com/updates/shows").mock(
        return_value=httpx.Response(200, json={"1": 100})
    )
    respx.get("https://api.tvmaze.com/shows/1").mock(
        return_value=httpx.Response(200, json=make_show(1, 100))
    )
    respx.get("https://api.tvmaze.com/shows/1/akas").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "name": "Alt Name DE",
                    "country": {"name": "Germany", "code": "DE", "timezone": "Europe/Berlin"},
                    "language": "de",
                },
                {"name": "Alt Name (no country)", "country": None, "language": None},
            ],
        )
    )

    run_id = await create_run(session, kind="initial")
    await session.commit()

    async with TVMazeClient(
        "https://api.tvmaze.com", rate_calls=50, rate_window=1, retry_base_delay=0.01
    ) as c:
        result = await run_initial_ingest(session_factory=lambda: session, client=c, run_id=run_id)

    assert result.shows_processed == 1
    assert result.shows_failed == 0

    akas = (await session.execute(select(m.ShowAka).where(m.ShowAka.show_id == 1))).scalars().all()
    assert {a.name for a in akas} == {"Alt Name DE", "Alt Name (no country)"}

    show = (
        await session.execute(
            select(m.Show).where(m.Show.id == 1),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()
    assert show.akas_synced_at is not None


@respx.mock
async def test_initial_ingest_soft_fails_on_akas_error(session):
    """When the AKAs endpoint returns 500, the show still persists but
    akas_synced_at stays NULL and shows_failed does not increment — the
    backfill orchestrator picks the show up later."""
    respx.get("https://api.tvmaze.com/updates/shows").mock(
        return_value=httpx.Response(200, json={"1": 100})
    )
    respx.get("https://api.tvmaze.com/shows/1").mock(
        return_value=httpx.Response(200, json=make_show(1, 100))
    )
    respx.get("https://api.tvmaze.com/shows/1/akas").mock(return_value=httpx.Response(500))

    run_id = await create_run(session, kind="initial")
    await session.commit()

    async with TVMazeClient(
        "https://api.tvmaze.com",
        rate_calls=50,
        rate_window=1,
        retry_max_attempts=1,
        retry_base_delay=0.001,
    ) as c:
        result = await run_initial_ingest(session_factory=lambda: session, client=c, run_id=run_id)

    assert result.shows_processed == 1
    assert result.shows_failed == 0

    show = (
        await session.execute(
            select(m.Show).where(m.Show.id == 1),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()
    assert show.akas_synced_at is None

    akas = (await session.execute(select(m.ShowAka).where(m.ShowAka.show_id == 1))).scalars().all()
    assert akas == []
