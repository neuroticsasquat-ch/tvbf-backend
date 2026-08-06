import re
from datetime import UTC, datetime

import httpx
import respx
from sqlalchemy import select

from tests.fixtures.tvmaze.show_factory import make_show
from tvbf.tvmaze import models as m
from tvbf.tvmaze.client import TVMazeClient
from tvbf.tvmaze.runs import create_run, finalize_run
from tvbf.tvmaze.update import run_update

_AKAS_URL_RE = re.compile(r"https://api\.tvmaze\.com/shows/\d+/akas")
_EPISODES_URL_RE = re.compile(r"https://api\.tvmaze\.com/shows/\d+/episodes")
_SEASON_EPISODES_URL_RE = re.compile(r"https://api\.tvmaze\.com/seasons/\d+/episodes")


def _mock_akas_default_empty() -> None:
    respx.get(url__regex=_AKAS_URL_RE).mock(return_value=httpx.Response(200, json=[]))


def _mock_episodes_default_empty() -> None:
    """Default-mock /episodes for any show id; merged with the embed, not replacing it."""
    respx.get(url__regex=_EPISODES_URL_RE).mock(return_value=httpx.Response(200, json=[]))


def _mock_season_episodes_default_empty() -> None:
    """Every updated show now has its seasons refetched for episode credits."""
    respx.get(url__regex=_SEASON_EPISODES_URL_RE).mock(return_value=httpx.Response(200, json=[]))


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
    _mock_episodes_default_empty()
    _mock_season_episodes_default_empty()
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
    _mock_episodes_default_empty()
    _mock_season_episodes_default_empty()
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
    _mock_episodes_default_empty()
    _mock_season_episodes_default_empty()
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

    async def broken_then_real(s, show, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated upsert failure")
        return await real_upsert(s, show, **kwargs)

    monkeypatch.setattr("tvbf.tvmaze.update.upsert_show_payload", broken_then_real)

    _mock_akas_default_empty()
    _mock_episodes_default_empty()
    _mock_season_episodes_default_empty()
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


def _season_episode(episode_id: int, *, cast=(), crew=()) -> dict:
    return {
        "id": episode_id,
        "season": 1,
        "number": 1,
        "_embedded": {
            "guestcast": [
                {
                    "person": {"id": pid, "name": f"Person {pid}", "updated": 1},
                    "character": {"id": cid, "name": f"Character {cid}"},
                }
                for pid, cid in cast
            ],
            "guestcrew": [
                {
                    "person": {"id": pid, "name": f"Person {pid}", "updated": 1},
                    "guestCrewType": role,
                }
                for pid, role in crew
            ],
        },
    }


@respx.mock
async def test_update_refetches_each_season_for_episode_credits(session):
    """The daily's season step. That the credits land at all is also the
    ordering proof: they FK to `episode.id`, so the show write must have
    committed before the season fetch runs."""
    respx.get("https://api.tvmaze.com/updates/shows").mock(
        return_value=httpx.Response(200, json={"1": 10})
    )
    respx.get("https://api.tvmaze.com/shows/1").mock(
        return_value=httpx.Response(200, json=make_show(1, 10, seasons=2, episodes_per_season=1))
    )
    _mock_akas_default_empty()
    _mock_episodes_default_empty()
    s1 = respx.get("https://api.tvmaze.com/seasons/1001/episodes").mock(
        return_value=httpx.Response(
            200, json=[_season_episode(10001, cast=[(10, 900)], crew=[(11, "Director")])]
        )
    )
    s2 = respx.get("https://api.tvmaze.com/seasons/1002/episodes").mock(
        return_value=httpx.Response(200, json=[_season_episode(10002)])
    )

    run_id = await create_run(session, kind="update")
    await session.commit()

    async with TVMazeClient(
        "https://api.tvmaze.com", rate_calls=50, rate_window=1, retry_base_delay=0.01
    ) as c:
        result = await run_update(session_factory=lambda: session, client=c, run_id=run_id)

    assert result.shows_processed == 1
    assert s1.call_count == 1 and s2.call_count == 1

    cast = (await session.execute(select(m.EpisodeGuestCast))).scalars().all()
    assert [(r.episode_id, r.person_id, r.character_id) for r in cast] == [(10001, 10, 900)]
    crew = (await session.execute(select(m.EpisodeCrew))).scalars().all()
    assert [(r.episode_id, r.person_id) for r in crew] == [(10001, 11)]

    seasons = (
        (
            await session.execute(
                select(m.Season).order_by(m.Season.id).execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    # Stamped even for the season that carried no credits at all — absence of
    # rows cannot stand in for "not yet fetched".
    assert all(s.credits_synced_at is not None for s in seasons)


@respx.mock
async def test_a_failing_season_does_not_fail_its_show(session):
    """The show is already committed and correct; the season keeps a NULL
    watermark and the backfill picks it up. Failing the show instead would be
    wrong here, because the daily's cursor advances past failed shows."""
    respx.get("https://api.tvmaze.com/updates/shows").mock(
        return_value=httpx.Response(200, json={"1": 10})
    )
    respx.get("https://api.tvmaze.com/shows/1").mock(
        return_value=httpx.Response(200, json=make_show(1, 10, seasons=1, episodes_per_season=1))
    )
    _mock_akas_default_empty()
    _mock_episodes_default_empty()
    respx.get("https://api.tvmaze.com/seasons/1001/episodes").mock(return_value=httpx.Response(500))

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
    assert result.shows_failed == 0
    season = (
        await session.execute(
            select(m.Season).where(m.Season.id == 1001).execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert season.credits_synced_at is None


@respx.mock
async def test_a_failing_season_is_handed_back_to_the_backfill(session):
    """A season that already synced once must have its watermark CLEARED when a
    later refresh fails. Left stamped, the backfill's `IS NULL` scan skips it
    and the daily has already advanced past this show — so nothing retries."""
    session.add(m.Show(id=1, name="Show 1", tvmaze_updated=1))
    await session.flush()
    session.add(m.Season(id=1001, show_id=1, number=1, credits_synced_at=datetime.now(UTC)))
    await session.commit()

    respx.get("https://api.tvmaze.com/updates/shows").mock(
        return_value=httpx.Response(200, json={"1": 10})
    )
    respx.get("https://api.tvmaze.com/shows/1").mock(
        return_value=httpx.Response(200, json=make_show(1, 10, seasons=1, episodes_per_season=1))
    )
    _mock_akas_default_empty()
    _mock_episodes_default_empty()
    respx.get("https://api.tvmaze.com/seasons/1001/episodes").mock(return_value=httpx.Response(500))

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
    assert result.shows_failed == 0
    season = (
        await session.execute(
            select(m.Season).where(m.Season.id == 1001).execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert season.credits_synced_at is None


@respx.mock
async def test_update_persists_specials_from_the_episodes_endpoint(session):
    """The daily delta maintains specials too — otherwise every show TV Maze
    flags as updated would silently lose the specials a backfill wrote."""
    respx.get("https://api.tvmaze.com/updates/shows").mock(
        return_value=httpx.Response(200, json={"1": 10})
    )
    respx.get("https://api.tvmaze.com/shows/1").mock(
        return_value=httpx.Response(200, json=make_show(1, 10, seasons=1, episodes_per_season=2))
    )
    episodes_route = respx.get("https://api.tvmaze.com/shows/1/episodes").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": 10001, "season": 1, "number": 1, "name": "S1E1"},
                {"id": 10002, "season": 1, "number": 2, "name": "S1E2"},
                {"id": 99999, "season": 1, "number": None, "name": "Behind the Scenes"},
            ],
        )
    )
    _mock_akas_default_empty()
    _mock_season_episodes_default_empty()

    run_id = await create_run(session, kind="update")
    await session.commit()

    async with TVMazeClient(
        "https://api.tvmaze.com", rate_calls=50, rate_window=1, retry_base_delay=0.01
    ) as c:
        result = await run_update(session_factory=lambda: session, client=c, run_id=run_id)

    assert result.shows_processed == 1
    assert episodes_route.calls.last.request.url.params.get_list("specials") == ["1"]

    eps = (await session.execute(select(m.Episode).where(m.Episode.show_id == 1))).scalars().all()
    assert {e.id for e in eps} == {10001, 10002, 99999}
    assert next(e for e in eps if e.id == 99999).number is None


@respx.mock
async def test_update_prunes_a_season_the_payload_has_dropped(session):
    """NEU-967 end-to-end on the daily path: the mirror converges on the payload.

    The show was mirrored with two seasons; upstream has since deleted the
    second. One daily cycle must leave the mirror matching what the payload
    now says, rather than carrying the phantom forever.
    """
    prior_run = await create_run(session, kind="initial")
    await session.commit()
    await finalize_run(session, prior_run, status="succeeded", last_update_cursor=1)

    # Seed the show as it was mirrored when upstream still had both seasons.
    respx.get("https://api.tvmaze.com/updates/shows").mock(
        return_value=httpx.Response(200, json={"1": 10})
    )
    respx.get("https://api.tvmaze.com/shows/1").mock(
        return_value=httpx.Response(200, json=make_show(1, 10, seasons=2, episodes_per_season=1))
    )
    _mock_akas_default_empty()
    _mock_episodes_default_empty()
    _mock_season_episodes_default_empty()

    seed_run = await create_run(session, kind="update")
    await session.commit()
    async with TVMazeClient(
        "https://api.tvmaze.com", rate_calls=50, rate_window=1, retry_base_delay=0.01
    ) as c:
        await run_update(session_factory=lambda: session, client=c, run_id=seed_run)

    seasons = (
        (await session.execute(select(m.Season.id).where(m.Season.show_id == 1))).scalars().all()
    )
    assert set(seasons) == {1001, 1002}

    # Upstream drops season 2 and marks the show updated again.
    respx.get("https://api.tvmaze.com/updates/shows").mock(
        return_value=httpx.Response(200, json={"1": 20})
    )
    respx.get("https://api.tvmaze.com/shows/1").mock(
        return_value=httpx.Response(200, json=make_show(1, 20, seasons=1, episodes_per_season=1))
    )

    run_id = await create_run(session, kind="update")
    await session.commit()
    async with TVMazeClient(
        "https://api.tvmaze.com", rate_calls=50, rate_window=1, retry_base_delay=0.01
    ) as c:
        result = await run_update(session_factory=lambda: session, client=c, run_id=run_id)

    assert result.shows_processed == 1
    seasons = (
        (await session.execute(select(m.Season.id).where(m.Season.show_id == 1))).scalars().all()
    )
    assert set(seasons) == {1001}, "the daily must drop a season the payload no longer names"


@respx.mock
async def test_update_tombstones_a_show_absent_from_the_feed(session):
    """NEU-1005 end-to-end on the daily path (ADR-0005)."""
    from tvbf.tvmaze.tombstone import _MIN_FEED_ABSOLUTE

    prior_run = await create_run(session, kind="initial")
    await session.commit()
    await finalize_run(session, prior_run, status="succeeded", last_update_cursor=1)

    # Mirrored but gone upstream.
    session.add(m.Show(id=4242, name="Cancelled Pilot", tvmaze_updated=5))
    await session.commit()

    # A feed big enough to clear the plausibility floors, naming show 1 but not 4242.
    feed = {"1": 10}
    feed.update({str(i): 1 for i in range(600_000, 600_000 + _MIN_FEED_ABSOLUTE)})
    respx.get("https://api.tvmaze.com/updates/shows").mock(
        return_value=httpx.Response(200, json=feed)
    )
    respx.get("https://api.tvmaze.com/shows/1").mock(
        return_value=httpx.Response(200, json=make_show(1, 10))
    )
    _mock_akas_default_empty()
    _mock_episodes_default_empty()
    _mock_season_episodes_default_empty()

    run_id = await create_run(session, kind="update")
    await session.commit()
    async with TVMazeClient(
        "https://api.tvmaze.com", rate_calls=50, rate_window=1, retry_base_delay=0.01
    ) as c:
        result = await run_update(session_factory=lambda: session, client=c, run_id=run_id)

    assert result.shows_processed == 1
    # A show the feed DOES name must stay live. Without this, a feed-key type
    # regression (int vs str) would tombstone the catalogue and still pass.
    live = (
        await session.execute(
            select(m.Show).where(m.Show.id == 1).execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert live.deleted_upstream_at is None, "a show named by the feed must stay live"

    row = (
        await session.execute(
            select(m.Show).where(m.Show.id == 4242).execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert row.deleted_upstream_at is not None, "absent from the feed means gone upstream"
    assert row.name == "Cancelled Pilot", "the row must survive — tombstone, not delete"


@respx.mock
async def test_an_aborted_update_tombstones_nothing(session):
    """A run that gave up partway never saw the whole catalogue, so it must not judge it."""
    from tvbf.tvmaze.tombstone import _MIN_FEED_ABSOLUTE

    prior_run = await create_run(session, kind="initial")
    await session.commit()
    await finalize_run(session, prior_run, status="succeeded", last_update_cursor=1)

    session.add(m.Show(id=4343, name="Still Here", tvmaze_updated=5))
    await session.commit()

    feed = {"1": 10, "2": 10}
    feed.update({str(i): 1 for i in range(600_000, 600_000 + _MIN_FEED_ABSOLUTE)})
    respx.get("https://api.tvmaze.com/updates/shows").mock(
        return_value=httpx.Response(200, json=feed)
    )
    # Every fetch 500s, so the run aborts on consecutive failures.
    # Matches /shows/{id} with its embed query, but not /shows/{id}/episodes.
    respx.get(url__regex=r"https://api\.tvmaze\.com/shows/\d+(\?|$)").mock(
        return_value=httpx.Response(500)
    )
    _mock_akas_default_empty()
    _mock_episodes_default_empty()
    _mock_season_episodes_default_empty()

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
            session_factory=lambda: session, client=c, run_id=run_id, failure_threshold=2
        )

    assert result.shows_processed == 0
    row = (
        await session.execute(
            select(m.Show).where(m.Show.id == 4343).execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert row.deleted_upstream_at is None, "an aborted run must not tombstone"


@respx.mock
async def test_an_implausible_feed_skips_tombstoning_but_the_daily_still_succeeds(session):
    """The guard must degrade to a skipped pass, not a failed run."""
    prior_run = await create_run(session, kind="initial")
    await session.commit()
    await finalize_run(session, prior_run, status="succeeded", last_update_cursor=1)

    session.add(m.Show(id=4444, name="Untouched", tvmaze_updated=5))
    await session.commit()

    # A feed far under the absolute floor — exactly the truncated-200 case.
    respx.get("https://api.tvmaze.com/updates/shows").mock(
        return_value=httpx.Response(200, json={"1": 10})
    )
    respx.get("https://api.tvmaze.com/shows/1").mock(
        return_value=httpx.Response(200, json=make_show(1, 10))
    )
    _mock_akas_default_empty()
    _mock_episodes_default_empty()
    _mock_season_episodes_default_empty()

    run_id = await create_run(session, kind="update")
    await session.commit()
    async with TVMazeClient(
        "https://api.tvmaze.com", rate_calls=50, rate_window=1, retry_base_delay=0.01
    ) as c:
        result = await run_update(session_factory=lambda: session, client=c, run_id=run_id)

    assert result.shows_processed == 1
    row = (
        await session.execute(
            select(m.IngestRun).where(m.IngestRun.id == run_id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()
    assert row.status == "succeeded", "an implausible feed must not fail the daily"

    show = (
        await session.execute(
            select(m.Show).where(m.Show.id == 4444).execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert show.deleted_upstream_at is None
