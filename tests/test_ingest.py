import httpx
import respx
from sqlalchemy import select

from tests.fixtures.tvmaze.show_factory import make_show
from tvbf.tvmaze import models as m
from tvbf.tvmaze.client import TVMazeClient
from tvbf.tvmaze.ingest import run_initial_ingest
from tvbf.tvmaze.runs import create_run


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
