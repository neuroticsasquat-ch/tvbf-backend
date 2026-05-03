import re

import httpx
import respx
from sqlalchemy import select

from tests.fixtures.tvmaze.show_factory import make_show
from tvbf.tvmaze import models as m
from tvbf.tvmaze.client import TVMazeClient
from tvbf.tvmaze.runs import create_run, finalize_run
from tvbf.tvmaze.update import run_update

_AKAS_URL_RE = re.compile(r"https://api\.tvmaze\.com/shows/\d+/akas")


def _mock_akas_default_empty() -> None:
    respx.get(url__regex=_AKAS_URL_RE).mock(return_value=httpx.Response(200, json=[]))


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

    _mock_akas_default_empty()
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

    _mock_akas_default_empty()
    run_id = await create_run(session, kind="update")
    await session.commit()

    async with TVMazeClient(
        "https://api.tvmaze.com", rate_calls=50, rate_window=1, retry_base_delay=0.01
    ) as c:
        result = await run_update(session_factory=lambda: session, client=c, run_id=run_id)

    assert result.shows_processed == 1
    assert result.last_update_cursor == 10


@respx.mock
async def test_update_aborts_after_consecutive_http_failures(session):
    respx.get("https://api.tvmaze.com/updates/shows").mock(
        return_value=httpx.Response(200, json={"1": 1, "2": 2, "3": 3})
    )
    respx.get("https://api.tvmaze.com/shows/1").mock(return_value=httpx.Response(500))
    respx.get("https://api.tvmaze.com/shows/2").mock(return_value=httpx.Response(500))
    respx.get("https://api.tvmaze.com/shows/3").mock(return_value=httpx.Response(500))

    _mock_akas_default_empty()
    run_id = await create_run(session, kind="update")
    await session.commit()

    async with TVMazeClient(
        "https://api.tvmaze.com",
        rate_calls=50,
        rate_window=1,
        retry_max_attempts=1,
        retry_base_delay=0.001,
    ) as c:
        result = await run_update(
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


@respx.mock
async def test_update_catches_upsert_errors_and_continues(session, monkeypatch):
    respx.get("https://api.tvmaze.com/updates/shows").mock(
        return_value=httpx.Response(200, json={"1": 10, "2": 20})
    )
    respx.get("https://api.tvmaze.com/shows/1").mock(
        return_value=httpx.Response(200, json=make_show(1, 10))
    )
    respx.get("https://api.tvmaze.com/shows/2").mock(
        return_value=httpx.Response(200, json=make_show(2, 20))
    )

    from tvbf.tvmaze import upsert as upsert_module

    real_upsert = upsert_module.upsert_show_payload
    call_count = {"n": 0}

    async def broken_then_real(s, show):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated upsert failure")
        return await real_upsert(s, show)

    monkeypatch.setattr("tvbf.tvmaze.update.upsert_show_payload", broken_then_real)

    _mock_akas_default_empty()
    run_id = await create_run(session, kind="update")
    await session.commit()

    async with TVMazeClient(
        "https://api.tvmaze.com",
        rate_calls=50,
        rate_window=1,
        retry_max_attempts=1,
        retry_base_delay=0.001,
    ) as c:
        result = await run_update(session_factory=lambda: session, client=c, run_id=run_id)

    assert result.shows_processed == 1
    assert result.shows_failed == 1
