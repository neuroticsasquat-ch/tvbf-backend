"""The season credits backfill (NEU-1512).

`test_credits_backfill.py`'s properties at one table: every season of a show
written, overflow included; a show stamped only when every season came back
with its `credits`; resumable; a 404 apart from real failures; no partial
writes; and a report that says how far it has got.

The series route honours `append_to_response` as measured: a `season/N` or
`season/N/credits` entry comes back only for a season the show has and the
caller asked for, and a standalone season answers `credits` inline.
"""

from datetime import UTC, datetime

import httpx
import pytest
import respx
from sqlalchemy import func, select

from tests.fixtures.tmdb.series_factory import (
    make_episode,
    make_season_credits,
    make_season_detail,
    make_season_regular,
    make_season_summary,
    make_series,
)
from tvbf.catalog import models as cm
from tvbf.tmdb.client import TMDBClient
from tvbf.tmdb.season_credits_backfill import (
    SeasonCreditsBackfillAborted,
    backfill_season_credits,
    build_report,
)

BASE = "https://api.themoviedb.org/3"

# Clear of the browse fixtures and of `test_credits_backfill.py`'s range.
_ID = 9_850_000


def _next_id() -> int:
    global _ID
    _ID += 1
    return _ID


def _season_tmdb_id(tmdb_id: int, number: int) -> int:
    return tmdb_id * 100 + number


def _regular(person: int, character: str = "Someone") -> dict:
    return make_season_regular(person, f"Person {person}", character)


def mock_series(
    tmdb_id: int,
    regulars: dict[int, list[dict]],
    *,
    without_credits: frozenset[int] = frozenset(),
) -> dict[int, respx.Route]:
    """Route one show: `regulars` maps each season number to its `credits.cast`.

    A season in `without_credits` comes back with no `credits` at all, on
    either request — the response failing to carry what was asked for.
    """
    payload = make_series(tmdb_id, seasons=0, append_seasons=False)
    payload["seasons"] = [
        make_season_summary(_season_tmdb_id(tmdb_id, n), n, episode_count=1)
        for n in sorted(regulars)
    ]

    def _block(number: int) -> dict:
        return make_season_detail(number, [make_episode(tmdb_id * 1000 + number, number, 1)])

    def _respond(request: httpx.Request) -> httpx.Response:
        body = dict(payload)
        for key in request.url.params.get("append_to_response", "").split(","):
            if not key.startswith("season/"):
                continue
            number_text, _, rest = key.removeprefix("season/").partition("/")
            number = int(number_text)
            if number not in regulars:
                continue
            if not rest:
                body[key] = _block(number)
            elif number not in without_credits:
                body[key] = make_season_credits(regulars[number])
        return httpx.Response(200, json=body)

    def _standalone(number: int) -> dict:
        block = _block(number) | {"id": _season_tmdb_id(tmdb_id, number)}
        if number not in without_credits:
            block["credits"] = make_season_credits(regulars[number])
        return block

    respx.get(f"{BASE}/tv/{tmdb_id}").mock(side_effect=_respond)
    return {
        number: respx.get(f"{BASE}/tv/{tmdb_id}/season/{number}").mock(
            return_value=httpx.Response(200, json=_standalone(number))
        )
        for number in regulars
    }


async def _mirror(
    session,
    tmdb_id: int,
    seasons: list[int],
    *,
    synced: bool = True,
    season_credits_synced: bool = False,
) -> int:
    """A mirrored show and its seasons — the spine the backfill writes onto."""
    stamp = datetime(2026, 9, 1, tzinfo=UTC)
    show_id = _next_id()
    session.add(
        cm.Show(
            id=show_id,
            name=f"Show {tmdb_id}",
            tmdb_id=tmdb_id,
            tmdb_synced_at=stamp if synced else None,
            credits_synced_at=stamp if synced else None,
            season_credits_synced_at=stamp if season_credits_synced else None,
        )
    )
    await session.flush()
    for number in seasons:
        session.add(
            cm.Season(
                id=_next_id(),
                show_id=show_id,
                season_number=number,
                tmdb_id=_season_tmdb_id(tmdb_id, number),
            )
        )
    await session.commit()
    return show_id


async def _run(session, **kwargs):
    async with TMDBClient(
        base_url=BASE,
        read_access_token="eyJ-not-a-real-token",
        rate_calls=200,
        rate_window=1,
        retry_base_delay=0.01,
    ) as client:
        return await backfill_season_credits(session, client, page_size=2, **kwargs)


async def _show(session, show_id: int) -> cm.Show:
    stmt = select(cm.Show).where(cm.Show.id == show_id).execution_options(populate_existing=True)
    return (await session.execute(stmt)).scalar_one()


async def _regulars(session, show_id: int) -> list[tuple[int, str]]:
    rows = await session.execute(
        select(cm.Season.season_number, cm.Person.name)
        .select_from(cm.SeasonCast)
        .join(cm.Season, cm.Season.id == cm.SeasonCast.season_id)
        .join(cm.Person, cm.Person.id == cm.SeasonCast.person_id)
        .where(cm.Season.show_id == show_id)
        .order_by(cm.Season.season_number, cm.Person.name)
    )
    return [tuple(row) for row in rows.all()]


# --- writing ----------------------------------------------------------------


@respx.mock
async def test_every_season_is_written_including_overflow_and_the_show_stamped(session):
    """Season 12 is past even the widened window this pass fetches with."""
    show_id = await _mirror(session, 1396, [1, 12])
    routes = mock_series(1396, {1: [_regular(1)], 12: [_regular(2)]})

    result = await _run(session)

    assert await _regulars(session, show_id) == [(1, "Person 1"), (12, "Person 2")]
    assert routes[12].call_count == 1
    assert routes[1].call_count == 0
    assert (await _show(session, show_id)).season_credits_synced_at is not None
    assert (result.shows_stamped, result.seasons_written) == (1, 2)


@respx.mock
async def test_the_request_spends_its_whole_budget_on_seasons(session):
    """No namespaces: this pass writes one table, and the freed slots put ten
    seasons on the first request instead of four."""
    await _mirror(session, 1396, [1])
    mock_series(1396, {1: [_regular(1)]})

    await _run(session)

    [call] = [c for c in respx.calls if c.request.url.path == "/3/tv/1396"]
    asked = call.request.url.params["append_to_response"].split(",")
    assert all(key.startswith("season/") for key in asked)
    assert "season/9/credits" in asked


@respx.mock
async def test_it_writes_nothing_but_season_regulars(session):
    """The other watermarks are not this pass's to move."""
    show_id = await _mirror(session, 1396, [1])
    before = await _show(session, show_id)
    was = (before.tmdb_synced_at, before.credits_synced_at)
    mock_series(1396, {1: [_regular(1)]})

    await _run(session)

    after = await _show(session, show_id)
    assert (after.tmdb_synced_at, after.credits_synced_at) == was
    assert (await session.execute(select(func.count()).select_from(cm.ShowCast))).scalar_one() == 0
    assert (await session.execute(select(func.count()).select_from(cm.Episode))).scalar_one() == 0


@respx.mock
async def test_a_season_that_came_back_without_credits_leaves_the_show_unstamped(session):
    """The request asked; an absent key describes the response, not the season."""
    show_id = await _mirror(session, 1396, [1, 2])
    mock_series(1396, {1: [_regular(1)], 2: [_regular(2)]}, without_credits=frozenset({2}))

    result = await _run(session)

    assert (await _show(session, show_id)).season_credits_synced_at is None
    assert await _regulars(session, show_id) == [], "season 1 must not land without its sibling"
    assert (result.shows_failed, result.seasons_without_credits) == (1, 1)


@respx.mock
async def test_a_show_with_no_regulars_in_any_season_is_stamped(session):
    """`cast: []` is upstream's answer, and a stamped show is not re-fetched."""
    show_id = await _mirror(session, 1396, [1])
    mock_series(1396, {1: []})

    first = await _run(session)
    second = await _run(session)

    assert (await _show(session, show_id)).season_credits_synced_at is not None
    assert (first.shows_stamped, first.seasons_written) == (1, 1)
    assert second.shows_considered == 0


# --- the work list ----------------------------------------------------------


@respx.mock
async def test_a_stamped_show_and_an_unmirrored_one_are_skipped(session):
    await _mirror(session, 1396, [1], season_credits_synced=True)
    await _mirror(session, 456, [1], synced=False)

    result = await _run(session)

    assert result.shows_considered == 0
    assert len(respx.calls) == 0


@respx.mock
async def test_an_interrupted_run_resumes_rather_than_restarting(session):
    first_id = await _mirror(session, 1396, [1])
    second_id = await _mirror(session, 456, [1])
    mock_series(1396, {1: [_regular(1)]})
    mock_series(456, {1: [_regular(2)]})

    partial = await _run(session, limit=1)
    rest = await _run(session)

    assert (partial.shows_considered, rest.shows_considered) == (1, 1)
    assert await _regulars(session, first_id) == [(1, "Person 1")]
    assert await _regulars(session, second_id) == [(1, "Person 2")]


# --- failures ---------------------------------------------------------------


@respx.mock
async def test_a_show_gone_upstream_is_stepped_over_and_left_unstamped(session):
    gone_id = await _mirror(session, 1, [1])
    good_id = await _mirror(session, 1396, [1])
    respx.get(f"{BASE}/tv/1").mock(return_value=httpx.Response(404))
    mock_series(1396, {1: [_regular(1)]})

    result = await _run(session, failure_threshold=1)

    assert (result.shows_failed, result.shows_gone, result.shows_stamped) == (1, 1, 1)
    assert (await _show(session, gone_id)).season_credits_synced_at is None
    assert (await _show(session, good_id)).season_credits_synced_at is not None


@respx.mock
async def test_consecutive_real_failures_abort_the_pass(session):
    for tmdb_id in (1, 2):
        await _mirror(session, tmdb_id, [1])
        respx.get(f"{BASE}/tv/{tmdb_id}").mock(return_value=httpx.Response(500))

    with pytest.raises(SeasonCreditsBackfillAborted):
        await _run(session, failure_threshold=2)


@respx.mock
async def test_a_failed_overflow_fetch_writes_nothing_for_the_show(session):
    show_id = await _mirror(session, 1396, [1, 12])
    mock_series(1396, {1: [_regular(1)], 12: [_regular(2)]})
    respx.get(f"{BASE}/tv/1396/season/12").mock(return_value=httpx.Response(500))

    result = await _run(session)

    assert result.shows_failed == 1
    assert await _regulars(session, show_id) == []
    assert (await _show(session, show_id)).season_credits_synced_at is None


# --- the report -------------------------------------------------------------


@respx.mock
async def test_report_counts_the_backlog_and_the_shows_with_no_regulars(session):
    done_id = await _mirror(session, 1396, [1])
    empty_id = await _mirror(session, 456, [1])
    await _mirror(session, 1667, [1])
    mock_series(1396, {1: [_regular(1)]})
    mock_series(456, {1: []})
    # A show with cast at show grain and no regular in any season.
    person = cm.Person(name="Guest", tmdb_id=77)
    session.add(person)
    await session.flush()
    session.add(cm.ShowCast(show_id=empty_id, person_id=person.id))
    await session.commit()

    await _run(session, limit=2)
    report = await build_report(session)

    assert (await _show(session, done_id)).season_credits_synced_at is not None
    assert report.to_dict() == {
        "shows_mirrored": 3,
        "shows_stamped": 2,
        "shows_remaining": 1,
        "stamped_with_no_regulars": 1,
        "season_cast_rows": 1,
    }
