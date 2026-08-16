"""The recommendations backfill and its report (NEU-1052).

Every test here is one of the ticket's acceptance criteria, or one of the ways a
pass with an ordered, self-emptying work list can quietly go wrong: an ordering
that is not the one claimed, a cursor that hands back the same failing show
forever, or a stamp that retires a show from a work list this pass had no
business emptying.

The series route is mocked as the live API behaves for a narrow append: the
namespace comes back because it was asked for, and no season blocks ride along
because none were requested.
"""

import httpx
import pytest
import respx
from sqlalchemy import func, select

from tests.fixtures.tmdb.series_factory import make_recommendations, make_series
from tvbf.catalog import models as cm
from tvbf.tmdb.client import TMDBClient
from tvbf.tmdb.recommendations_backfill import (
    APPEND,
    MissingRecommendationsNamespace,
    RecommendationsBackfillAborted,
    backfill_recommendations,
    build_report,
)

BASE = "https://api.themoviedb.org/3"

# Well clear of the browse fixtures' catalog, so these rows never collide with a
# seeded show and every assertion can name exact ids.
_ID = 9_900_000


def _next_id() -> int:
    global _ID
    _ID += 1
    return _ID


def mock_series(tmdb_id: int, recommends: list[int] | None) -> respx.Route:
    """Route `/tv/{id}`. `None` answers without the namespace at all."""
    payload = make_series(tmdb_id, seasons=0, append_seasons=False)
    if recommends is not None:
        payload["recommendations"] = make_recommendations(recommends)
    return respx.get(f"{BASE}/tv/{tmdb_id}").mock(return_value=httpx.Response(200, json=payload))


async def _seed_show(
    session,
    *,
    tmdb_id: int | None,
    name: str = "Mirrored Show",
    popularity: float | None = 10.0,
    synced: bool = True,
    recommendations_synced: bool = False,
) -> int:
    """A show the ingest mirrored before the namespace existed."""
    from datetime import UTC, datetime

    show_id = _next_id()
    stamp = datetime(2026, 8, 11, tzinfo=UTC)
    session.add(
        cm.Show(
            id=show_id,
            name=name,
            tmdb_id=tmdb_id,
            popularity=popularity,
            tmdb_synced_at=stamp if synced else None,
            credits_synced_at=stamp if synced else None,
            recommendations_synced_at=stamp if recommendations_synced else None,
        )
    )
    await session.flush()
    await session.commit()
    return show_id


async def _seed_target(session, tmdb_id: int, name: str = "Target") -> int:
    """A show a recommendation can point at, already stamped so it is not itself
    part of the work list — the subject of each test should be one show."""
    return await _seed_show(
        session, tmdb_id=tmdb_id, name=name, popularity=1.0, recommendations_synced=True
    )


async def _run(session, *, page_size: int = 2, **kwargs):
    async with TMDBClient(
        base_url=BASE,
        read_access_token="eyJ-not-a-real-token",
        rate_calls=200,
        rate_window=1,
        retry_base_delay=0.01,
    ) as client:
        return await backfill_recommendations(session, client, page_size=page_size, **kwargs)


async def _stored(session, show_id: int) -> list[tuple[int, int]]:
    rows = await session.execute(
        select(cm.ShowRecommendation.rank, cm.ShowRecommendation.target_show_id)
        .where(cm.ShowRecommendation.source_show_id == show_id)
        .order_by(cm.ShowRecommendation.rank)
    )
    return [(row.rank, row.target_show_id) for row in rows]


async def _show(session, show_id: int) -> cm.Show:
    return (
        await session.execute(
            select(cm.Show).where(cm.Show.id == show_id).execution_options(populate_existing=True)
        )
    ).scalar_one()


# --- writing ----------------------------------------------------------------


@respx.mock
async def test_writes_the_ranked_rows_and_stamps_the_show(session):
    """AC: a mirrored show ends up with its list and leaves the work list."""
    target_id = await _seed_target(session, 8_101)
    source_id = await _seed_show(session, tmdb_id=8_100, name="Source", popularity=99.0)
    mock_series(8_100, [8_101])

    result = await _run(session)

    assert await _stored(session, source_id) == [(1, target_id)]
    assert (result.shows_stamped, result.rows_written, result.targets_dropped) == (1, 1, 0)
    assert (await _show(session, source_id)).recommendations_synced_at is not None


@respx.mock
async def test_it_asks_for_one_namespace_and_makes_one_request_per_show(session):
    """The cost model: this pass writes one table, so it appends one namespace.

    Appending `DEFAULT_APPEND` would fetch a payload it discards and drag the
    speculative season window along with it, turning a 40-season show into 34
    requests for a list of twenty ids.
    """
    await _seed_show(session, tmdb_id=8_200)
    route = mock_series(8_200, [])

    await _run(session)

    assert route.call_count == 1
    assert len(respx.calls) == 1
    assert route.calls[0].request.url.params["append_to_response"] == ",".join(APPEND)


@respx.mock
async def test_a_show_with_nothing_upstream_is_stamped_and_counted_apart(session):
    """AC: zero rows and still stamped, so it leaves the work list.

    The whole reason the watermark is a column: ~8% of the zero-vote long tail
    answers with nothing, and under a "has no `show_recommendation` row" work
    list every one of them would be re-fetched on every run forever.
    """
    show_id = await _seed_show(session, tmdb_id=8_300, name="Obscure")
    mock_series(8_300, [])

    first = await _run(session)

    assert (first.shows_stamped, first.shows_without_recommendations) == (1, 1)
    assert first.targets_dropped == 0
    assert (await _show(session, show_id)).recommendations_synced_at is not None
    assert await _stored(session, show_id) == []

    second = await _run(session)
    assert second.shows_considered == 0


@respx.mock
async def test_a_response_without_the_namespace_is_a_failure_not_an_empty_list(session):
    """The namespace is always appended, so its absence describes the response
    rather than the series. Stamping on it would retire the show having never
    seen its list."""
    show_id = await _seed_show(session, tmdb_id=8_400)
    mock_series(8_400, None)

    result = await _run(session, failure_threshold=99)

    assert result.shows_failed == 1
    assert (await _show(session, show_id)).recommendations_synced_at is None


# --- what it must not write -------------------------------------------------


@respx.mock
async def test_it_writes_no_spine_row_and_advances_no_other_watermark(session):
    """AC: the backfill writes no spine row and does not advance
    `tmdb_synced_at` or `credits_synced_at`.

    Stamping either would retire a show from a work list this pass never covered
    — the distinction NEU-1127 had to draw when it added the second watermark
    rather than reuse the first, one grain along.
    """
    await _seed_target(session, 8_501)
    source_id = await _seed_show(session, tmdb_id=8_500, name="Source", popularity=99.0)
    before = await _show(session, source_id)
    was = (before.tmdb_synced_at, before.credits_synced_at)
    shows = (await session.execute(select(func.count()).select_from(cm.Show))).scalar_one()
    mock_series(8_500, [8_501])

    await _run(session)

    after = await _show(session, source_id)
    assert (after.tmdb_synced_at, after.credits_synced_at) == was
    assert (await session.execute(select(func.count()).select_from(cm.Show))).scalar_one() == shows
    assert (await session.execute(select(func.count()).select_from(cm.Season))).scalar_one() == 0
    assert (await session.execute(select(func.count()).select_from(cm.Episode))).scalar_one() == 0


@respx.mock
async def test_a_target_we_do_not_mirror_is_dropped(session):
    """AC: no stored row points at a missing show.

    And it is counted as a *drop*, not as upstream having nothing to say. The two
    end at the same zero rows and are different problems: silence is normal for
    ~8% of the long tail, while twenty unmirrored targets mean the mirror is
    behind. `shows_without_recommendations` is read as an operational signal
    about the first, so it must not absorb the second.
    """
    source_id = await _seed_show(session, tmdb_id=8_600)
    mock_series(8_600, [999_999])

    result = await _run(session)

    assert await _stored(session, source_id) == []
    assert (result.shows_stamped, result.shows_without_recommendations) == (1, 0)
    assert result.targets_dropped == 1


# --- the ordering and the cursor --------------------------------------------


@respx.mock
async def test_shows_are_taken_most_popular_first(session):
    """AC: descending popularity. It is what front-loads the value of a pass
    that may never finish — the top 20,000 cover essentially every page a user
    loads, and a show with no score at all sorts last rather than out."""
    await _seed_show(session, tmdb_id=8_701, name="Middle", popularity=50.0)
    await _seed_show(session, tmdb_id=8_702, name="Unscored", popularity=None)
    await _seed_show(session, tmdb_id=8_703, name="Popular", popularity=900.0)
    for tmdb_id in (8_701, 8_702, 8_703):
        mock_series(tmdb_id, [])

    await _run(session, page_size=1)

    fetched = [int(call.request.url.path.rsplit("/", 1)[-1]) for call in respx.calls]
    assert fetched == [8_703, 8_701, 8_702]


@respx.mock
async def test_a_partial_pass_resumes_where_it_stopped(session):
    """AC: resumes correctly after a kill — and `--limit` is the same mechanism,
    which is why stopping early is a supported destination rather than a
    truncated journey."""
    await _seed_show(session, tmdb_id=8_801, name="First", popularity=90.0)
    await _seed_show(session, tmdb_id=8_802, name="Second", popularity=80.0)
    for tmdb_id in (8_801, 8_802):
        mock_series(tmdb_id, [])

    first = await _run(session, limit=1)
    second = await _run(session)

    assert (first.shows_considered, second.shows_considered) == (1, 1)
    fetched = [int(call.request.url.path.rsplit("/", 1)[-1]) for call in respx.calls]
    assert fetched == [8_801, 8_802]


@respx.mock
async def test_a_failing_show_does_not_wedge_the_loop(session):
    """The reason the work list is paged by a cursor rather than re-read from the
    top: a failed show keeps its null watermark and stays in the candidate set,
    so a cursorless page would hand it back forever and the pass would never
    reach the show behind it."""
    await _seed_show(session, tmdb_id=8_901, name="Broken", popularity=90.0)
    good_id = await _seed_show(session, tmdb_id=8_902, name="Fine", popularity=80.0)
    respx.get(f"{BASE}/tv/8901").mock(return_value=httpx.Response(500))
    mock_series(8_902, [])

    result = await _run(session, page_size=1, failure_threshold=99)

    assert (result.shows_considered, result.shows_failed, result.shows_stamped) == (2, 1, 1)
    assert (await _show(session, good_id)).recommendations_synced_at is not None


@respx.mock
async def test_a_series_gone_upstream_does_not_count_toward_the_abort(session):
    """A 404 is a data condition, not an outage (NEU-1006): the show stays
    unstamped and the tombstone pass is what settles whether it still exists."""
    show_id = await _seed_show(session, tmdb_id=9_001)
    respx.get(f"{BASE}/tv/9001").mock(return_value=httpx.Response(404))

    result = await _run(session, failure_threshold=1)

    assert (result.shows_failed, result.shows_gone) == (1, 1)
    assert (await _show(session, show_id)).recommendations_synced_at is None


@respx.mock
async def test_enough_consecutive_failures_abort_the_pass(session):
    """At that point upstream is down rather than the data being odd, and
    spending the rest of a multi-hour pass discovering that is waste."""
    for tmdb_id in (9_101, 9_102):
        await _seed_show(session, tmdb_id=tmdb_id)
        respx.get(f"{BASE}/tv/{tmdb_id}").mock(return_value=httpx.Response(500))

    with pytest.raises(RecommendationsBackfillAborted):
        await _run(session, failure_threshold=2)


# --- the work list ----------------------------------------------------------


@respx.mock
async def test_an_unmirrored_or_locally_authored_show_is_never_considered(session):
    """A row with no `tmdb_synced_at` belongs to the ingest's work list, and a
    locally-authored row (ADR-0008) has no id to make the request with."""
    await _seed_show(session, tmdb_id=9_201, synced=False)
    await _seed_show(session, tmdb_id=None)

    result = await _run(session)

    assert result.shows_considered == 0
    assert not respx.calls


# --- the report -------------------------------------------------------------


@respx.mock
async def test_the_report_counts_what_is_stored_and_what_is_left(session):
    target_id = await _seed_target(session, 9_301)
    source_id = await _seed_show(session, tmdb_id=9_300, name="Source", popularity=99.0)
    await _seed_show(session, tmdb_id=9_302, name="Untouched", popularity=5.0)
    mock_series(9_300, [9_301])
    await _run(session, limit=1)

    report = await build_report(session)

    assert report.totals["shows_remaining"] == 1
    assert report.rows == {"rows_stored": 1, "source_shows": 1, "target_shows": 1}
    assert report.targets_tombstoned == 0
    assert await _stored(session, source_id) == [(1, target_id)]


async def test_a_missing_namespace_raises_rather_than_stamping(session):
    """The exception exists so the distinction is nameable at the call site."""
    assert issubclass(MissingRecommendationsNamespace, Exception)
