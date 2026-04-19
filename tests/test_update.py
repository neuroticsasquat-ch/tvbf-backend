import httpx
import respx

from tests.fixtures.tvmaze.show_factory import make_show
from tvbf.tvmaze import models as m
from tvbf.tvmaze.client import TVMazeClient
from tvbf.tvmaze.runs import create_run, finalize_run
from tvbf.tvmaze.update import run_update


@respx.mock
async def test_update_only_fetches_shows_past_cursor(session):
    prior_run = await create_run(session, kind="initial")
    await session.commit()
    await finalize_run(session, prior_run, status="succeeded", last_update_cursor=100)
    session.add(m.Show(id=1, name="pre", tvmaze_updated=100))
    await session.commit()

    respx.get("https://api.tvmaze.com/updates/shows").mock(
        return_value=httpx.Response(200, json={"1": 100, "2": 150, "3": 200})
    )
    old = respx.get("https://api.tvmaze.com/shows/1").mock(
        return_value=httpx.Response(200, json=make_show(1, 100))
    )
    s2 = respx.get("https://api.tvmaze.com/shows/2").mock(
        return_value=httpx.Response(200, json=make_show(2, 150))
    )
    s3 = respx.get("https://api.tvmaze.com/shows/3").mock(
        return_value=httpx.Response(200, json=make_show(3, 200))
    )

    run_id = await create_run(session, kind="update")
    await session.commit()

    async with TVMazeClient(
        "https://api.tvmaze.com", rate_calls=50, rate_window=1, retry_base_delay=0.01
    ) as c:
        result = await run_update(session_factory=lambda: session, client=c, run_id=run_id)

    assert result.shows_processed == 2
    assert result.last_update_cursor == 200
    assert old.call_count == 0
    assert s2.call_count == 1
    assert s3.call_count == 1


@respx.mock
async def test_update_with_no_prior_run_treats_cursor_as_zero(session):
    respx.get("https://api.tvmaze.com/updates/shows").mock(
        return_value=httpx.Response(200, json={"1": 10})
    )
    respx.get("https://api.tvmaze.com/shows/1").mock(
        return_value=httpx.Response(200, json=make_show(1, 10))
    )

    run_id = await create_run(session, kind="update")
    await session.commit()

    async with TVMazeClient(
        "https://api.tvmaze.com", rate_calls=50, rate_window=1, retry_base_delay=0.01
    ) as c:
        result = await run_update(session_factory=lambda: session, client=c, run_id=run_id)

    assert result.shows_processed == 1
    assert result.last_update_cursor == 10
