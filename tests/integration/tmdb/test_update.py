"""The daily catalog delta from `/tv/changes` (NEU-1035).

What has to hold across a job nobody watches: that a long gap is walked rather
than requested in one piece, that a changed show is genuinely re-fetched rather
than skipped for already being synced, that the cursor advances only on a run
that finished, and that the first delta after a full pass knows where to start.

The `/tv/changes` route is mocked per window — the feed's whole shape is
`{results: [{id, adult}], page, total_pages}`, and the pagination is a real
behaviour of it rather than an incidental one, so pages are served for real.
"""

from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
import respx
from sqlalchemy import select, update

from tests.integration.tmdb.test_ingest import BASE, _run_row, _show, mock_series
from tvbf.catalog import models as m
from tvbf.config import get_settings
from tvbf.tmdb.client import TMDBClient
from tvbf.tmdb.update import date_to_cursor, run_catalog_update, run_catalog_update_job
from tvbf.tvmaze import models as tvm
from tvbf.tvmaze.runs import create_run, finalize_run

TODAY = date(2026, 8, 10)


def mock_changes(windows: dict[tuple[str, str], list[list[int]]]) -> respx.Route:
    """Route `/tv/changes`, serving each window's pages from `windows`.

    Keyed by `(start_date, end_date)` and valued by a list of pages, so a window
    with two pages is `[[1, 2], [3]]`. An unlisted window answers empty, which is
    what a quiet stretch of the gap looks like.
    """

    def _respond(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        key = (params["start_date"], params["end_date"])
        pages = windows.get(key, [[]])
        page = int(params.get("page", 1))
        results = pages[page - 1] if page <= len(pages) else []
        return httpx.Response(
            200,
            json={
                "results": [{"id": series_id, "adult": False} for series_id in results],
                "page": page,
                "total_pages": len(pages),
                "total_results": sum(len(p) for p in pages),
            },
        )

    return respx.get(f"{BASE}/tv/changes").mock(side_effect=_respond)


async def _run(session, *, today: date = TODAY, export_ids=(), **kwargs):
    """One delta cycle.

    `export_ids` defaults to empty rather than to the real download: the
    tombstone pass that rides along is exercised in `test_tombstone.py`, and an
    empty export trips its absolute floor so nothing is written. That is what
    keeps every test in this file off the export host without pretending the
    step is not there.
    """
    run_id = await create_run(session, kind="catalog_update")
    await session.commit()
    async with TMDBClient(
        base_url=BASE,
        read_access_token="eyJ-not-a-real-token",
        rate_calls=200,
        rate_window=1,
        retry_base_delay=0.01,
    ) as client:
        result = await run_catalog_update(
            session_factory=lambda: session,
            client=client,
            run_id=run_id,
            today=today,
            export_ids=export_ids,
            **kwargs,
        )
    return run_id, result


async def _seed_cursor(session, day: date) -> None:
    """A prior delta that covered up to `day`."""
    run_id = await create_run(session, kind="catalog_update")
    await finalize_run(session, run_id, status="succeeded", last_update_cursor=date_to_cursor(day))
    await session.commit()


# --- the gap ----------------------------------------------------------------


@respx.mock
async def test_a_thirty_day_gap_is_walked_in_windows(session):
    """The ticket's acceptance criterion, end to end: a container down for a
    month catches up in consecutive ≤14-day requests, not one invalid range."""
    await _seed_cursor(session, TODAY - timedelta(days=30))
    changes = mock_changes({("2026-07-11", "2026-07-25"): [[1396]]})
    mock_series(1396, [1])

    _, result = await _run(session)

    windows = [
        (c.request.url.params["start_date"], c.request.url.params["end_date"])
        for c in changes.calls
    ]
    assert windows == [
        ("2026-07-11", "2026-07-25"),
        ("2026-07-25", "2026-08-08"),
        ("2026-08-08", "2026-08-10"),
    ]
    assert result.shows_processed == 1


@respx.mock
async def test_pages_are_followed(session):
    """Unlike TV Maze's feed, this one pages — a delta that read only page one
    would silently drop most of a busy day."""
    await _seed_cursor(session, TODAY - timedelta(days=1))
    mock_changes({("2026-08-09", "2026-08-10"): [[1396], [456], [1399]]})
    for series_id in (1396, 456, 1399):
        mock_series(series_id, [1])

    _, result = await _run(session)

    assert result.shows_processed == 3
    assert {s.tmdb_id for s in (await session.execute(select(m.Show))).scalars()} == {
        1396,
        456,
        1399,
    }


@respx.mock
async def test_a_window_past_tmdbs_page_cap_is_halved_rather_than_truncated(session):
    """500 pages is 50,000 changed series — out of reach for a day, in reach for
    the 14-day windows a long gap is walked in. Truncating would drop the
    overflow and *still* advance the cursor past those days."""
    await _seed_cursor(session, TODAY - timedelta(days=14))

    def _respond(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        start, end = params["start_date"], params["end_date"]
        # Only the full 14-day window overflows; either half is servable.
        overflowing = (start, end) == ("2026-07-27", "2026-08-10")
        return httpx.Response(
            200,
            json={
                "results": [] if overflowing else [{"id": 1396, "adult": False}],
                "page": 1,
                "total_pages": 900 if overflowing else 1,
            },
        )

    changes = respx.get(f"{BASE}/tv/changes").mock(side_effect=_respond)
    mock_series(1396, [1])

    run_id, result = await _run(session)

    windows = [
        (c.request.url.params["start_date"], c.request.url.params["end_date"])
        for c in changes.calls
    ]
    assert windows == [
        ("2026-07-27", "2026-08-10"),
        ("2026-07-27", "2026-08-03"),
        ("2026-08-03", "2026-08-10"),
    ]
    assert result.shows_processed == 1
    assert (await _run_row(session, run_id)).status == "succeeded"


@respx.mock
async def test_a_single_day_past_the_page_cap_fails_the_run(session):
    """Nothing left to halve. Unrepresentable is a thing to fail on — finalising
    `succeeded` here would advance the cursor past a day never read."""
    await _seed_cursor(session, TODAY - timedelta(days=1))
    respx.get(f"{BASE}/tv/changes").mock(
        return_value=httpx.Response(200, json={"results": [], "page": 1, "total_pages": 900})
    )

    with pytest.raises(RuntimeError, match="cannot be split further"):
        await _run(session)

    # It raises out of the delta, and `run_catalog_update_job` turns that into a
    # `failed` run — so the cursor is never written and the day is re-covered.
    live = (
        (
            await session.execute(
                select(tvm.IngestRun)
                .where(tvm.IngestRun.kind == "catalog_update")
                .order_by(tvm.IngestRun.started_at.desc())
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .first()
    )
    assert live is not None
    assert live.last_update_cursor is None


@respx.mock
async def test_the_job_wrapper_marks_an_unrepresentable_window_failed(session, monkeypatch):
    """The exit code has to reflect it: a delta that could not read a day must
    not report success to Coolify."""
    monkeypatch.setenv("TMDB_READ_ACCESS_TOKEN", "eyJ-not-a-real-token")
    get_settings.cache_clear()
    # Relative to the real clock: the job resolves `today` itself.
    await _seed_cursor(session, datetime.now(UTC).date() - timedelta(days=1))
    respx.get(f"{BASE}/tv/changes").mock(
        return_value=httpx.Response(200, json={"results": [], "page": 1, "total_pages": 900})
    )

    run_id = await create_run(session, kind="catalog_update")
    await session.commit()
    try:
        await run_catalog_update_job(run_id, get_settings())
    finally:
        get_settings.cache_clear()

    row = await _run_row(session, run_id)
    assert row.status == "failed"
    assert "cannot be split further" in (row.error or "")
    assert row.last_update_cursor is None


@respx.mock
async def test_a_series_named_by_two_windows_is_fetched_once(session):
    """Windows share their boundary day so nothing falls between them. The
    duplicate that produces must not become a duplicate request."""
    await _seed_cursor(session, TODAY - timedelta(days=20))
    mock_changes(
        {
            ("2026-07-21", "2026-08-04"): [[1396]],
            ("2026-08-04", "2026-08-10"): [[1396]],
        }
    )
    mock_series(1396, [1])

    _, result = await _run(session)

    assert result.shows_processed == 1
    series_calls = [c for c in respx.calls if str(c.request.url).startswith(f"{BASE}/tv/1396?")]
    assert len(series_calls) == 1


# --- the re-fetch -----------------------------------------------------------


@respx.mock
async def test_a_changed_show_is_re_fetched_even_though_it_is_already_synced(session):
    """The full pass's work list skips a synced show; this one must not. A
    changed id says nothing about *what* changed, so there is no cheaper path —
    and skipping would leave the row stale forever."""
    await _seed_cursor(session, TODAY - timedelta(days=1))
    mock_changes({("2026-08-09", "2026-08-10"): [[1396]]})
    mock_series(1396, [1, 2])
    session.add(
        m.Show(id=4821, tmdb_id=1396, name="stale", tmdb_synced_at=datetime(2026, 1, 1, tzinfo=UTC))
    )
    await session.commit()

    _, result = await _run(session)

    assert result.shows_processed == 1
    show = await _show(session, 1396)
    assert show.id == 4821, "the preserved id is what app.user_show_watch points at"
    assert show.name == "Show 1396"
    assert show.tmdb_synced_at is not None
    assert show.tmdb_synced_at > datetime(2026, 1, 1, tzinfo=UTC)


@respx.mock
async def test_a_series_new_to_the_catalog_is_added(session):
    await _seed_cursor(session, TODAY - timedelta(days=1))
    mock_changes({("2026-08-09", "2026-08-10"): [[1396]]})
    mock_series(1396, [1])

    _, result = await _run(session)

    assert result.shows_processed == 1
    assert (await _show(session, 1396)).tmdb_synced_at is not None


@respx.mock
async def test_a_quiet_day_costs_no_series_request(session):
    await _seed_cursor(session, TODAY - timedelta(days=1))
    mock_changes({})

    run_id, result = await _run(session)

    assert (result.shows_processed, result.shows_failed) == (0, 0)
    assert (await _run_row(session, run_id)).status == "succeeded"


# --- the cursor -------------------------------------------------------------


@respx.mock
async def test_a_successful_run_records_the_day_it_covered(session):
    await _seed_cursor(session, TODAY - timedelta(days=1))
    mock_changes({("2026-08-09", "2026-08-10"): [[1396]]})
    mock_series(1396, [1])

    run_id, _ = await _run(session)

    assert (await _run_row(session, run_id)).last_update_cursor == date_to_cursor(TODAY)


@respx.mock
async def test_an_aborted_run_leaves_the_cursor_where_it_was(session):
    """The next run must re-cover the whole gap, not step over the part this one
    never reached."""
    await _seed_cursor(session, TODAY - timedelta(days=1))
    mock_changes({("2026-08-09", "2026-08-10"): [list(range(1, 11))]})
    for series_id in range(1, 11):
        respx.get(f"{BASE}/tv/{series_id}").mock(return_value=httpx.Response(500))

    run_id, result = await _run(session, failure_threshold=3)

    assert result.aborted
    row = await _run_row(session, run_id)
    assert row.status == "failed"
    assert row.last_update_cursor is None


@respx.mock
async def test_the_first_delta_bootstraps_from_the_full_pass(session):
    """`catalog_initial` writes no cursor, so the first delta reads its
    `started_at` — the pass's beginning, because a series mirrored in its first
    hour could have changed in its eighth."""
    pass_id = await create_run(session, kind="catalog_initial")
    await session.execute(
        update(tvm.IngestRun)
        .where(tvm.IngestRun.id == pass_id)
        .values(started_at=datetime(2026, 8, 3, 6, 0, tzinfo=UTC))
    )
    await finalize_run(session, pass_id, status="succeeded")
    await session.commit()
    changes = mock_changes({})

    await _run(session)

    assert changes.calls[0].request.url.params["start_date"] == "2026-08-03"


@respx.mock
async def test_the_bootstrap_covers_a_resumed_pass_from_its_first_attempt(session):
    """The full pass is resumable, so it is routinely several runs of which only
    the last succeeds — and a show mirrored by the first attempt was stamped days
    before the last one started. Bootstrapping from the successful run would step
    over every change to those shows, and nothing would pick them up: they carry
    `tmdb_synced_at`, so the pass's own work list excludes them too.
    """
    for started, status in (
        (datetime(2026, 8, 1, 3, 0, tzinfo=UTC), "cancelled"),
        (datetime(2026, 8, 2, 3, 0, tzinfo=UTC), "cancelled"),
        (datetime(2026, 8, 3, 3, 0, tzinfo=UTC), "succeeded"),
    ):
        run_id = await create_run(session, kind="catalog_initial")
        await session.execute(
            update(tvm.IngestRun).where(tvm.IngestRun.id == run_id).values(started_at=started)
        )
        await finalize_run(session, run_id, status=status)
    await session.commit()
    changes = mock_changes({})

    await _run(session)

    assert changes.calls[0].request.url.params["start_date"] == "2026-08-01"


@respx.mock
async def test_a_pass_that_never_succeeded_does_not_bootstrap_the_delta(session, caplog):
    """A half-finished pass covered a half-finished catalog. Claiming its window
    would claim coverage it never achieved."""
    run_id = await create_run(session, kind="catalog_initial")
    await session.execute(
        update(tvm.IngestRun)
        .where(tvm.IngestRun.id == run_id)
        .values(started_at=datetime(2026, 8, 1, 3, 0, tzinfo=UTC))
    )
    await finalize_run(session, run_id, status="failed")
    await session.commit()
    changes = mock_changes({})

    with caplog.at_level("WARNING", logger="tvbf.tmdb.update"):
        await _run(session)

    assert changes.calls[0].request.url.params["start_date"] == "2026-08-09"
    assert "no completed full catalog pass" in caplog.text


@respx.mock
async def test_a_cold_start_covers_only_the_last_day_and_says_so(session, caplog):
    """No cursor and no completed pass means there is no window to bound — a
    delta running before the full pass it is meant to follow."""
    changes = mock_changes({})

    with caplog.at_level("WARNING", logger="tvbf.tmdb.update"):
        await _run(session)

    assert changes.calls[0].request.url.params["start_date"] == "2026-08-09"
    assert "no completed full catalog pass" in caplog.text


@respx.mock
async def test_the_tv_maze_lineage_is_not_read_as_a_date(session):
    """One column, several lineages. A TV Maze epoch read as this cursor would
    decode to 1970 and request a fifty-year gap a window at a time."""
    tvmaze_run = await create_run(session, kind="update")
    await finalize_run(session, tvmaze_run, status="succeeded", last_update_cursor=1754784000)
    await session.commit()
    changes = mock_changes({})

    await _run(session)

    # Nothing in this lineage, so the cold-start floor applies — not the epoch
    # the TV Maze daily happens to have left in the column.
    assert changes.calls[0].request.url.params["start_date"] == "2026-08-09"


# --- failure handling -------------------------------------------------------


@respx.mock
async def test_a_series_gone_upstream_does_not_count_toward_the_abort(session):
    """A show deleted between the change and the re-fetch is a data condition,
    not a broken upstream (NEU-1006)."""
    await _seed_cursor(session, TODAY - timedelta(days=1))
    mock_changes({("2026-08-09", "2026-08-10"): [[*range(1, 6), 1396]]})
    for series_id in range(1, 6):
        respx.get(f"{BASE}/tv/{series_id}").mock(return_value=httpx.Response(404))
    mock_series(1396, [1])

    run_id, result = await _run(session, failure_threshold=3)

    assert (result.shows_gone, result.shows_processed) == (5, 1)
    assert (await _run_row(session, run_id)).status == "succeeded"


@respx.mock
async def test_one_bad_series_does_not_stop_the_ones_after_it(session):
    await _seed_cursor(session, TODAY - timedelta(days=1))
    mock_changes({("2026-08-09", "2026-08-10"): [[1, 1396]]})
    respx.get(f"{BASE}/tv/1").mock(return_value=httpx.Response(500))
    mock_series(1396, [1])

    _, result = await _run(session, failure_threshold=10)

    assert (result.shows_processed, result.shows_failed) == (1, 1)
