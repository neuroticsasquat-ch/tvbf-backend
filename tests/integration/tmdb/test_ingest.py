"""The full TMDB catalog ingest (NEU-1034).

The properties an operator has to be able to rely on across a multi-hour pass:
that a killed run resumes rather than restarts, that "resume" means the right
thing given the migration already put rows in this table, that a show too large
for one request still lands complete, that a single bad series cannot take the
run down, and that nothing here touches a locally-authored row.

The series route is mocked with a side effect that honours `append_to_response`
the way the live API was **measured** to — appended `season/N` entries a show
does not have are dropped from the response silently (2026-08-10,
`scripts/probe_tmdb_season_speculation.py`). A mock that returned them anyway
would make the reconcile-and-overflow path untestable.
"""

import re

import httpx
import respx
from sqlalchemy import func, select, update

from tests.fixtures.tmdb.series_factory import (
    make_episode,
    make_season_detail,
    make_season_summary,
    make_series,
)
from tvbf.catalog import models as m
from tvbf.catalog.runs import create_run
from tvbf.tmdb.client import TMDBClient
from tvbf.tmdb.ingest import run_catalog_ingest

BASE = "https://api.themoviedb.org/3"
_SEASON_URL_RE = re.compile(rf"{re.escape(BASE)}/tv/\d+/season/\d+")


def _series_payload(tmdb_id: int, season_numbers: list[int], *, episodes: int = 1) -> dict:
    """A `/tv/{id}` body with no appended seasons — the caller's mock adds them."""
    payload = make_series(tmdb_id, seasons=0, append_seasons=False)
    payload["seasons"] = [
        make_season_summary(tmdb_id * 100 + n, n, episode_count=episodes) for n in season_numbers
    ]
    payload["number_of_seasons"] = len([n for n in season_numbers if n != 0])
    return payload


def _season_block(
    tmdb_id: int, number: int, *, episodes: int = 1, standalone: bool = False
) -> dict:
    block = make_season_detail(
        number,
        [
            make_episode(tmdb_id * 10000 + number * 100 + e, number, e)
            for e in range(1, episodes + 1)
        ],
    )
    if standalone:
        # `GET /tv/{id}/season/{n}` carries a real `id`; the appended form does
        # not. Both are exercised, because a show splits across the two.
        block["id"] = tmdb_id * 100 + number
    return block


def mock_series(tmdb_id: int, season_numbers: list[int], *, episodes: int = 1, **overrides):
    """Route `/tv/{id}` and every `/tv/{id}/season/{n}` for one show.

    The series route honours `append_to_response` exactly as measured: it
    returns a `season/N` block only for seasons the show actually has *and* the
    caller asked for.
    """

    def _respond(request: httpx.Request) -> httpx.Response:
        payload = _series_payload(tmdb_id, season_numbers, episodes=episodes) | overrides
        asked = request.url.params.get("append_to_response", "").split(",")
        for key in asked:
            if not key.startswith("season/"):
                continue
            number = int(key.removeprefix("season/"))
            if number in season_numbers:
                payload[key] = _season_block(tmdb_id, number, episodes=episodes)
        return httpx.Response(200, json=payload)

    respx.get(f"{BASE}/tv/{tmdb_id}").mock(side_effect=_respond)
    for number in season_numbers:
        respx.get(f"{BASE}/tv/{tmdb_id}/season/{number}").mock(
            return_value=httpx.Response(
                200, json=_season_block(tmdb_id, number, episodes=episodes, standalone=True)
            )
        )


async def _run(session, series_ids, **kwargs):
    run_id = await create_run(session, kind="catalog_initial")
    await session.commit()
    async with TMDBClient(
        base_url=BASE,
        read_access_token="eyJ-not-a-real-token",
        rate_calls=200,
        rate_window=1,
        retry_base_delay=0.01,
    ) as client:
        result = await run_catalog_ingest(
            session_factory=lambda: session,
            client=client,
            run_id=run_id,
            series_ids=series_ids,
            **kwargs,
        )
    return run_id, result


async def _show(session, tmdb_id: int) -> m.Show:
    return (
        await session.execute(
            select(m.Show)
            .where(m.Show.tmdb_id == tmdb_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def _run_row(session, run_id) -> m.IngestRun:
    return (
        await session.execute(
            select(m.IngestRun)
            .where(m.IngestRun.id == run_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


# --- the happy path ---------------------------------------------------------


@respx.mock
async def test_every_series_in_the_export_is_mirrored(session):
    mock_series(1396, [1, 2])
    mock_series(456, [1])

    _, result = await _run(session, [1396, 456])

    assert (result.shows_processed, result.shows_failed) == (2, 0)
    assert {s.tmdb_id for s in (await session.execute(select(m.Show))).scalars()} == {1396, 456}
    assert (await session.execute(select(func.count()).select_from(m.Episode))).scalar_one() == 3


@respx.mock
async def test_the_run_finalises_without_a_cursor(session):
    """TV Maze's initial ingest handed a per-show epoch to the first daily. TMDB's
    delta is a date range (NEU-1035), so there is no epoch — and a value written
    into a column typed for TV Maze's would be one the next reader misreads."""
    mock_series(1396, [1])

    run_id, _ = await _run(session, [1396])

    row = await _run_row(session, run_id)
    assert row.status == "succeeded"
    assert row.last_update_cursor is None


@respx.mock
async def test_progress_is_observable_while_the_run_is_in_flight(session):
    mock_series(1396, [1])
    mock_series(456, [1])

    run_id, _ = await _run(session, [1396, 456])

    row = await _run_row(session, run_id)
    assert row.shows_processed == 2
    assert row.last_progress_at is not None


# --- resumability -----------------------------------------------------------


@respx.mock
async def test_a_synced_show_is_not_refetched(session):
    """A killed run resumes rather than restarts."""
    mock_series(1396, [1])
    mock_series(456, [1])

    await _run(session, [1396, 456])
    calls_after_first = len(respx.calls)

    _, result = await _run(session, [1396, 456])

    assert result.shows_processed == 0
    assert len(respx.calls) == calls_after_first, "a synced show must cost no request"


@respx.mock
async def test_a_copied_and_enriched_row_is_ingested_in_place(session):
    """The reason the watermark is a column rather than row-existence.

    NEU-1042 copied ~89k TV Maze shows into `catalog` and NEU-1043 mapped a
    `tmdb_id` onto ~63k of them. Those rows exist and are correctly identified
    while still holding **TV Maze data** — resuming on row-existence would skip
    exactly the shows users track, and finish reporting success.
    """
    session.add(
        m.Show(id=4821, tmdb_id=1396, name="Breaking Bad (from TV Maze)", match_method="tvdb_id")
    )
    await session.commit()
    mock_series(1396, [1])

    _, result = await _run(session, [1396])

    assert result.shows_processed == 1
    show = await _show(session, 1396)
    # The preserved id is what `app.user_show_watch` points at (ADR-0008).
    assert show.id == 4821
    assert show.name == "Show 1396"
    assert show.tmdb_synced_at is not None


@respx.mock
async def test_a_locally_authored_row_is_never_touched(session):
    """`tmdb_id IS NULL` is the sanctioned way to hold a show TMDB does not list.
    It is in no export, so it is in no work list."""
    session.add(m.Show(id=9001, tmdb_id=None, name="Only Ours"))
    await session.commit()
    mock_series(1396, [1])

    await _run(session, [1396])

    local = (
        await session.execute(
            select(m.Show).where(m.Show.id == 9001).execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert local.name == "Only Ours"
    assert local.tmdb_id is None
    assert local.tmdb_synced_at is None


# --- the append budget ------------------------------------------------------


@respx.mock
async def test_a_forty_season_show_is_fetched_completely(session):
    """The ticket's acceptance criterion. Nine season slots ride the series
    request; the other 32 are follow-ups."""
    numbers = list(range(1, 41))
    mock_series(1396, numbers)

    _, result = await _run(session, [1396])

    assert result.shows_processed == 1
    show = await _show(session, 1396)
    seasons = (
        await session.execute(select(m.Season.season_number).where(m.Season.show_id == show.id))
    ).scalars()
    assert sorted(seasons) == numbers
    episodes = (
        await session.execute(
            select(func.count()).select_from(m.Episode).where(m.Episode.show_id == show.id)
        )
    ).scalar_one()
    assert episodes == 40

    season_calls = [c for c in respx.calls if _SEASON_URL_RE.fullmatch(str(c.request.url))]
    # Seasons 1..8 ride the speculative window (0 is asked for and does not
    # exist, which TMDB drops silently); 9..40 are the overflow.
    assert len(season_calls) == 32


@respx.mock
async def test_a_show_inside_the_window_costs_exactly_one_request(session):
    """What the ~3.2-hour estimate rests on."""
    mock_series(1396, [0, 1, 2])

    await _run(session, [1396])

    assert len(respx.calls) == 1


@respx.mock
async def test_an_overflow_season_failure_leaves_the_show_unsynced(session):
    """A partial show must not be stamped as done.

    Writing a show with one season's episodes missing and then setting the
    watermark would retire it from every future work list — the silent partial
    the watermark exists to prevent. Failing the whole show leaves it for the
    next run.
    """
    numbers = list(range(1, 12))
    mock_series(1396, numbers)
    respx.get(f"{BASE}/tv/1396/season/11").mock(return_value=httpx.Response(500))

    _, result = await _run(session, [1396])

    assert (result.shows_processed, result.shows_failed) == (0, 1)
    assert (await session.execute(select(func.count()).select_from(m.Show))).scalar_one() == 0


# --- failure handling -------------------------------------------------------


@respx.mock
async def test_a_series_gone_upstream_does_not_count_toward_the_abort(session):
    """The export lists ids `/tv/{id}` no longer serves. A data condition, not a
    broken upstream (NEU-1006) — counting it would wedge the pass."""
    for series_id in range(1, 21):
        respx.get(f"{BASE}/tv/{series_id}").mock(return_value=httpx.Response(404))
    mock_series(1396, [1])

    run_id, result = await _run(session, [*range(1, 21), 1396], failure_threshold=3)

    assert result.shows_gone == 20
    assert result.shows_processed == 1
    assert (await _run_row(session, run_id)).status == "succeeded"


@respx.mock
async def test_the_progress_log_separates_gone_from_real_failures(session, caplog):
    """`ingest_run.shows_failed` is one column and counts both, so over ~229k ids
    an operator polling the run row cannot tell a thousand deleted series from a
    thousand broken requests. The log line is what makes that legible."""
    respx.get(f"{BASE}/tv/1").mock(return_value=httpx.Response(404))
    respx.get(f"{BASE}/tv/2").mock(return_value=httpx.Response(500))
    mock_series(1396, [1])

    with caplog.at_level("INFO", logger="tvbf.tmdb.ingest"):
        await _run(session, [1, 2, 1396], failure_threshold=10)

    assert "1/3 processed, 2 failed (1 gone upstream, 1 real)" in caplog.text


@respx.mock
async def test_consecutive_real_failures_abort_the_run(session):
    for series_id in range(1, 11):
        respx.get(f"{BASE}/tv/{series_id}").mock(return_value=httpx.Response(500))
    mock_series(1396, [1])

    run_id, result = await _run(session, [*range(1, 11), 1396], failure_threshold=3)

    assert result.shows_processed == 0
    row = await _run_row(session, run_id)
    assert row.status == "failed"
    assert "consecutive failures" in (row.error or "")


@respx.mock
async def test_one_bad_series_does_not_stop_the_ones_after_it(session):
    respx.get(f"{BASE}/tv/1").mock(return_value=httpx.Response(500))
    mock_series(1396, [1])

    _, result = await _run(session, [1, 1396], failure_threshold=10)

    assert (result.shows_processed, result.shows_failed) == (1, 1)


# --- coverage ---------------------------------------------------------------


@respx.mock
async def test_every_approved_namespace_is_persisted(session):
    """The ticket's spot-check, made mechanical.

    "Do not trim this list to what we need today" is the point of the project:
    with `append_to_response` a namespace costs a column, where under TV Maze it
    cost a multi-hour backfill. A namespace fetched and dropped on the floor is
    the failure this catches.
    """
    mock_series(
        1396,
        [1],
        **{
            "external_ids": {"imdb_id": "tt0903747", "tvdb_id": 81189},
            "alternative_titles": {"results": [{"iso_3166_1": "DE", "title": "Breaking Bad DE"}]},
            "content_ratings": {"results": [{"iso_3166_1": "US", "rating": "TV-MA"}]},
            "keywords": {"results": [{"id": 271, "name": "drug dealer"}]},
            "translations": {
                "translations": [
                    {
                        "iso_639_1": "de",
                        "iso_3166_1": "DE",
                        "name": "Deutsch",
                        "english_name": "German",
                        "data": {"name": "Breaking Bad", "overview": "Ein Chemielehrer."},
                    }
                ]
            },
            "images": {"backdrops": [{"file_path": "/b.jpg"}], "logos": [], "posters": []},
            "videos": {"results": [{"id": "abc123", "key": "yt-key", "site": "YouTube"}]},
            "episode_groups": {"results": [{"id": "grp1", "name": "Absolute", "type": 2}]},
            "watch/providers": {
                "results": {
                    "US": {
                        "link": "https://justwatch.example/us",
                        "flatrate": [
                            {"provider_id": 8, "provider_name": "Netflix", "display_priority": 1}
                        ],
                    }
                }
            },
            "screened_theatrically": {"results": []},
        },
    )

    await _run(session, [1396])

    show = await _show(session, 1396)
    assert show.tvdb_id == 81189
    assert show.imdb_id == "tt0903747"

    async def _count(model) -> int:
        return (
            await session.execute(
                select(func.count()).select_from(model).where(model.show_id == show.id)
            )
        ).scalar_one()

    landed = {
        "show_aka": await _count(m.ShowAka),
        "content_rating": await _count(m.ContentRating),
        "show_keyword": await _count(m.ShowKeyword),
        "translation": await _count(m.Translation),
        "image": await _count(m.Image),
        "video": await _count(m.Video),
        "episode_group": await _count(m.EpisodeGroup),
        "show_watch_provider": await _count(m.ShowWatchProvider),
    }
    assert all(landed.values()), f"namespaces fetched but not persisted: {landed}"


@respx.mock
async def test_seasons_the_payload_no_longer_names_are_pruned(session):
    """ADR-0004 ports: the series body is authoritative for the season set."""
    mock_series(1396, [1, 2])
    await _run(session, [1396])

    show = await _show(session, 1396)
    # Clear the watermark so the second pass picks the show up again — the same
    # state a killed run leaves behind.
    await session.execute(update(m.Show).where(m.Show.id == show.id).values(tmdb_synced_at=None))
    await session.commit()
    respx.routes.clear()
    mock_series(1396, [1])

    await _run(session, [1396])

    seasons = (
        await session.execute(select(m.Season.season_number).where(m.Season.show_id == show.id))
    ).scalars()
    assert sorted(seasons) == [1]


@respx.mock
async def test_a_copied_season_survives_the_prune(session):
    """A copied season carries `tmdb_id IS NULL` and `app.user_episode_watch`
    points at its episodes' preserved ids. Deleting it would destroy watch
    history nothing upstream could restore — reconciling the two grains is
    NEU-1045's, not this pass's."""
    session.add(m.Show(id=4821, tmdb_id=1396, name="from TV Maze"))
    await session.flush()
    session.add(m.Season(id=77, tmdb_id=None, show_id=4821, season_number=1))
    await session.commit()
    mock_series(1396, [1, 2])

    await _run(session, [1396])

    copied = (
        await session.execute(
            select(m.Season).where(m.Season.id == 77).execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    assert copied is not None, "the prune must step around a locally-authored season"
