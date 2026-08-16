"""The daily trending snapshot (NEU-1055).

The ticket's three acceptance criteria are all about what a *reader* can see —
never a half-written list, a `captured_at` that describes the fetch, and a
previous snapshot that survives a failure — so every test here is written from
the stored table's point of view rather than the pass's return value, which
would agree with itself either way.

The endpoint is mocked as the live one behaves: a `results` array of series
objects, most trending first, of which this pass reads one field.
"""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from tvbf.catalog import models as cm
from tvbf.catalog.runs import create_run
from tvbf.tmdb.client import TMDBClient
from tvbf.tmdb.trending import (
    TRENDING_WINDOW,
    ranked_series_ids,
    replace_snapshot,
    run_trending_snapshot,
    run_trending_snapshot_job,
)

BASE = "https://api.themoviedb.org/3"

# Well clear of the browse fixtures' catalog, so these rows never collide with a
# seeded show and every assertion can name exact ids.
_ID = 9_800_000


def _next_id() -> int:
    global _ID
    _ID += 1
    return _ID


def mock_trending(*tmdb_ids: int, window: str = TRENDING_WINDOW) -> respx.Route:
    """Route `/trending/tv/{window}` with the given series, in rank order."""
    return respx.get(f"{BASE}/trending/tv/{window}").mock(
        return_value=httpx.Response(
            200,
            json={
                "page": 1,
                "results": [
                    {"id": i, "name": f"Series {i}", "popularity": 100.0} for i in tmdb_ids
                ],
            },
        )
    )


async def _seed_show(session, tmdb_id: int, name: str = "Mirrored Show") -> int:
    show_id = _next_id()
    session.add(cm.Show(id=show_id, name=name, tmdb_id=tmdb_id))
    await session.flush()
    await session.commit()
    return show_id


async def _stored(session) -> list[tuple[int, int]]:
    rows = await session.execute(
        select(cm.TrendingShow.rank, cm.TrendingShow.show_id).order_by(cm.TrendingShow.rank)
    )
    return [(row.rank, row.show_id) for row in rows]


async def _captured_at(session) -> list[datetime]:
    rows = await session.execute(select(cm.TrendingShow.captured_at))
    return list(rows.scalars().all())


async def _run(session, **kwargs):
    """One snapshot against a run row of its own.

    The pass owns its transactions through the factory, so the factory hands it
    the test's session — the shape `test_ingest` and `test_update` already use —
    and the run row is created here so the finalized status is readable
    afterwards.
    """
    run_id = await create_run(session, kind="trending_snapshot")
    await session.commit()

    async with TMDBClient(
        base_url=BASE,
        read_access_token="eyJ-not-a-real-token",
        rate_calls=200,
        rate_window=1,
        retry_base_delay=0.01,
    ) as client:
        result = await run_trending_snapshot(
            session_factory=lambda: session, client=client, run_id=run_id, **kwargs
        )
    return run_id, result


async def _run_status(session, run_id) -> str:
    return (
        await session.execute(select(cm.IngestRun.status).where(cm.IngestRun.id == run_id))
    ).scalar_one()


# --- the snapshot -----------------------------------------------------------


@respx.mock
async def test_it_stores_the_list_in_tmdbs_own_order(session):
    first = await _seed_show(session, 7_001, "Most Trending")
    second = await _seed_show(session, 7_002, "Also Trending")
    mock_trending(7_001, 7_002)

    _, result = await _run(session)

    assert await _stored(session) == [(1, first), (2, second)]
    assert (result.stored, result.offered, result.unresolved) == (2, 2, 0)


@respx.mock
async def test_it_asks_for_the_week_window(session):
    """`week` over `day` is the ticket's one product decision: the job runs
    daily either way, so `day` would buy volatility rather than freshness."""
    await _seed_show(session, 7_010)
    route = mock_trending(7_010)

    await _run(session)

    assert route.called
    assert route.calls[0].request.url.path.endswith("/trending/tv/week")


@respx.mock
async def test_a_run_replaces_the_previous_snapshot_whole(session):
    """AC 1. TMDB's ranking is a total order, so a merge would interleave two
    vintages of it — yesterday's number three surviving under today's list."""
    old = await _seed_show(session, 7_020, "Yesterday")
    new = await _seed_show(session, 7_021, "Today")
    mock_trending(7_020)
    await _run(session)
    respx.reset()
    mock_trending(7_021)

    await _run(session)

    assert await _stored(session) == [(1, new)]
    assert old not in [show_id for _, show_id in await _stored(session)]


@respx.mock
async def test_captured_at_is_the_fetch_not_the_write(session):
    """AC 2. It is what NEU-1056's seven-day cutoff is measured against, so a
    write-time stamp would credit the snapshot with however long the pass took."""
    await _seed_show(session, 7_030)
    mock_trending(7_030)
    before = datetime.now(UTC)

    _, result = await _run(session)

    after = datetime.now(UTC)
    stamps = await _captured_at(session)
    assert stamps and all(before <= s <= after for s in stamps)
    assert result.captured_at == stamps[0]


@respx.mock
async def test_every_row_of_one_snapshot_shares_one_captured_at(session):
    """The table holds one snapshot, not a history — a reader asking how old the
    list is must not get a different answer per row."""
    await _seed_show(session, 7_040)
    await _seed_show(session, 7_041)
    mock_trending(7_040, 7_041)

    await _run(session)

    assert len(set(await _captured_at(session))) == 1


@respx.mock
async def test_the_swap_is_invisible_until_it_commits(session, test_engine):
    """AC 1, the half an end-state assertion cannot reach: *mid-run*.

    A delete that committed before its insert would leave the same rows behind
    and pass every other test here, while serving an empty list to whoever asked
    in between. So this one reads from a second connection while the first is
    still inside the transaction.
    """
    yesterday = await _seed_show(session, 7_110, "Yesterday")
    today = await _seed_show(session, 7_111, "Today")
    mock_trending(7_110)
    await _run(session)

    onlooker = async_sessionmaker(test_engine, expire_on_commit=False)
    await replace_snapshot(session, ranked=[(1, 7_111)], captured_at=datetime.now(UTC))

    async with onlooker() as other:
        assert await _stored(other) == [(1, yesterday)]

    await session.commit()
    async with onlooker() as other:
        assert await _stored(other) == [(1, today)]


# --- dropping ---------------------------------------------------------------


@respx.mock
async def test_an_unmirrored_entry_is_dropped_and_leaves_its_rank_behind(session, caplog):
    """The project-wide rule: if a user cannot click it and add it to My Shows,
    it does not appear. Rank keeps meaning TMDB's position, so the survivors are
    not renumbered — and the drop is logged at error, because a nonzero count is
    an ingest problem rather than a trending one."""
    first = await _seed_show(session, 7_050)
    third = await _seed_show(session, 7_052)
    mock_trending(7_050, 7_051, 7_052)

    with caplog.at_level("ERROR"):
        _, result = await _run(session)

    assert await _stored(session) == [(1, first), (3, third)]
    assert (result.stored, result.offered, result.unresolved) == (2, 3, 1)
    assert "7051" in caplog.text.replace(",", "").replace(" ", "")


@respx.mock
async def test_a_show_listed_twice_keeps_its_best_rank(session):
    """`uq_trending_show_show` would refuse the second row; deduplicating here is
    what makes that constraint a statement about the data rather than a way for
    a run to die."""
    show_id = await _seed_show(session, 7_060)
    mock_trending(7_060, 7_060)

    _, result = await _run(session)

    assert await _stored(session) == [(1, show_id)]
    assert (result.stored, result.duplicated, result.unresolved) == (1, 1, 0)


@respx.mock
async def test_a_tombstoned_show_still_enters_the_snapshot(session):
    """`adult` and `deleted_upstream_at` are read-time filters (NEU-1053,
    NEU-1108). Copying them here would make a resurrected show invisible until
    the next snapshot, and would confuse "not mirrored" — an ingest defect worth
    logging — with "mirrored and not shown", which is not one."""
    show_id = _next_id()
    session.add(
        cm.Show(
            id=show_id,
            name="Tombstoned",
            tmdb_id=7_070,
            deleted_upstream_at=datetime.now(UTC) - timedelta(days=1),
        )
    )
    await session.flush()
    await session.commit()
    mock_trending(7_070)

    _, result = await _run(session)

    assert await _stored(session) == [(1, show_id)]
    assert result.unresolved == 0


# --- failure ----------------------------------------------------------------


@respx.mock
async def test_a_snapshot_that_resolves_nothing_leaves_the_previous_one_standing(session, caplog):
    """AC 3, in its sharpest form: replacing a usable list with an empty one is
    the single outcome worse than serving yesterday's."""
    kept = await _seed_show(session, 7_080)
    mock_trending(7_080)
    await _run(session)
    respx.reset()
    mock_trending(7_081)

    with caplog.at_level("ERROR"):
        run_id, result = await _run(session)

    assert await _stored(session) == [(1, kept)]
    assert result.stored == 0 and result.skipped_reason is not None
    assert await _run_status(session, run_id) == "failed"


@respx.mock
async def test_an_upstream_failure_leaves_the_previous_snapshot_intact(session):
    """AC 3. The write happens after the fetch, so there is nothing to undo —
    which is the point: the transaction is never opened."""
    kept = await _seed_show(session, 7_090)
    mock_trending(7_090)
    await _run(session)
    respx.reset()
    respx.get(f"{BASE}/trending/tv/{TRENDING_WINDOW}").mock(return_value=httpx.Response(500))

    with pytest.raises(httpx.HTTPStatusError):
        await _run(session)

    assert await _stored(session) == [(1, kept)]


@respx.mock
async def test_a_successful_run_is_finalized_succeeded(session):
    await _seed_show(session, 7_100)
    mock_trending(7_100)

    run_id, _ = await _run(session)

    assert await _run_status(session, run_id) == "succeeded"


@respx.mock
async def test_the_run_row_counts_only_the_unmirrored_entries(session):
    """`shows_failed` is the durable record of the count the ticket asks an
    operator to read as an ingest defect. A duplicate is not one, so folding the
    two together would make the only number that outlives the log wrong."""
    await _seed_show(session, 7_120)
    mock_trending(7_120, 7_120, 7_121)

    run_id, result = await _run(session)

    row = (
        await session.execute(
            select(cm.IngestRun.shows_processed, cm.IngestRun.shows_failed)
            .where(cm.IngestRun.id == run_id)
            .execution_options(populate_existing=True)
        )
    ).one()
    assert (result.stored, result.unresolved, result.duplicated) == (1, 1, 1)
    assert (row.shows_processed, row.shows_failed) == (1, 1)


@respx.mock
async def test_an_upstream_crash_finalizes_the_run_failed(session, monkeypatch):
    """The scheduled entrypoint reads the run's terminal status to decide its own
    exit code, so an exception escaping the pass without finalizing would leave a
    `running` row and report success to Coolify."""
    from tvbf.config import get_settings

    kept = await _seed_show(session, 7_130)
    mock_trending(7_130)
    await _run(session)
    respx.reset()
    respx.get(f"{BASE}/trending/tv/{TRENDING_WINDOW}").mock(return_value=httpx.Response(500))

    run_id = await create_run(session, kind="trending_snapshot")
    await session.commit()
    await run_trending_snapshot_job(
        run_id,
        get_settings().model_copy(
            update={
                "tmdb_base_url": BASE,
                "tmdb_read_access_token": "eyJ-not-a-real-token",
                "tmdb_retry_max_attempts": 1,
            }
        ),
    )

    assert await _run_status(session, run_id) == "failed"
    assert await _stored(session) == [(1, kept)]


# --- parsing ----------------------------------------------------------------


def test_a_malformed_entry_is_skipped_and_leaves_a_rank_gap():
    """Somebody else's feed: losing one entry from a list of twenty beats losing
    the day's snapshot. The rank comes from the position, so the gap is visible
    rather than papered over."""
    body = {"results": [{"id": 1}, {"name": "no id"}, "not a dict", {"id": True}, {"id": 5}]}

    assert list(ranked_series_ids(body)) == [(1, 1), (5, 5)]


def test_an_empty_or_missing_results_array_yields_nothing():
    assert list(ranked_series_ids({})) == []
    assert list(ranked_series_ids({"results": None})) == []
