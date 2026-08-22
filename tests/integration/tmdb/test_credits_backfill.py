"""The credits backfill and its report (NEU-1127).

Every test here is one of the ticket's acceptance criteria, or one of the ways
this pass could quietly do the thing it exists *not* to do — write outside the
four credit tables and their three lookups, over a spine users are now reading
from.

The series route is mocked the way the live API was measured to behave: an
appended `season/N` a show does not have is dropped silently, and the appended
block carries the same `guest_stars` / `crew` the standalone season fetch does
(`scripts/probe_tmdb_episode_credits_append.py`). So the pass runs through the
real speculate-then-reconcile path rather than a simplified one.
"""

import uuid
from datetime import UTC, datetime

import httpx
import pytest
import respx
from sqlalchemy import func, select

from tests.fixtures.handles import new_handle
from tests.fixtures.tmdb.series_factory import (
    make_aggregate_credits,
    make_cast_member,
    make_crew_member,
    make_episode,
    make_episode_crew_member,
    make_guest_star,
    make_job,
    make_role,
    make_season_detail,
    make_season_summary,
    make_series,
)
from tvbf.app.models import User, UserEpisodeWatch, UserShowWatch
from tvbf.catalog import models as cm
from tvbf.tmdb.client import TMDBClient
from tvbf.tmdb.credits_backfill import (
    CreditsBackfillAborted,
    backfill_credits,
    build_report,
)

BASE = "https://api.themoviedb.org/3"

# Well clear of the browse fixtures' catalog, so these rows never collide with a
# seeded show and every assertion can name exact ids.
_ID = 9_800_000


def _next_id() -> int:
    global _ID
    _ID += 1
    return _ID


def _cast() -> list[dict]:
    return [make_cast_member(501, "Bryan Cranston", [make_role("Walter White", 62)], order=0)]


def _crew() -> list[dict]:
    return [make_crew_member(601, "Vince Gilligan", "Writing", [make_job("Writer", 62)])]


def _guests() -> list[dict]:
    return [make_guest_star(701, "Krysten Ritter", "Jane Margolis")]


def _episode_crew() -> list[dict]:
    return [make_episode_crew_member(801, "Michelle MacLaren", "Directing", "Director")]


def _series_payload(
    tmdb_id: int,
    *,
    seasons: dict[int, list[int]],
    cast: list[dict] | None = None,
    crew: list[dict] | None = None,
    guests: list[dict] | None = None,
    episode_crew: list[dict] | None = None,
) -> dict:
    """A `/tv/{id}` body naming `{season_number: [episode numbers]}`, no appends yet."""
    payload = make_series(tmdb_id, seasons=0, append_seasons=False)
    payload["seasons"] = [
        make_season_summary(tmdb_id * 100 + number, number, episode_count=len(numbers))
        for number, numbers in sorted(seasons.items())
    ]
    payload["aggregate_credits"] = make_aggregate_credits(
        cast=cast if cast is not None else _cast(),
        crew=crew if crew is not None else _crew(),
        tmdb_id=tmdb_id,
    )
    payload["_guests"] = guests if guests is not None else _guests()
    payload["_episode_crew"] = episode_crew if episode_crew is not None else _episode_crew()
    return payload


def _season_block(payload: dict, tmdb_id: int, number: int, episode_numbers: list[int]) -> dict:
    return make_season_detail(
        number,
        [
            make_episode(
                _upstream_episode_id(tmdb_id, number, e),
                number,
                e,
                guest_stars=payload["_guests"],
                crew=payload["_episode_crew"],
            )
            for e in episode_numbers
        ],
    )


def _upstream_episode_id(tmdb_id: int, season: int, number: int) -> int:
    return tmdb_id * 10_000 + season * 100 + number


def mock_series(tmdb_id: int, seasons: dict[int, list[int]], **kwargs) -> dict[int, respx.Route]:
    """Route `/tv/{id}` and every `/tv/{id}/season/{n}` for one show.

    The series route honours `append_to_response` exactly as measured: a
    `season/N` block comes back only for a season the show has *and* the caller
    asked for. Seasons outside the speculative window have to be fetched
    standalone, which is what the season routes are for.
    """
    template = _series_payload(tmdb_id, seasons=seasons, **kwargs)

    def _respond(request: httpx.Request) -> httpx.Response:
        payload = {k: v for k, v in template.items() if not k.startswith("_")}
        for key in request.url.params.get("append_to_response", "").split(","):
            if not key.startswith("season/"):
                continue
            number = int(key.removeprefix("season/"))
            if number in seasons:
                payload[key] = _season_block(template, tmdb_id, number, seasons[number])
        return httpx.Response(200, json=payload)

    respx.get(f"{BASE}/tv/{tmdb_id}").mock(side_effect=_respond)
    return {
        number: respx.get(f"{BASE}/tv/{tmdb_id}/season/{number}").mock(
            return_value=httpx.Response(200, json=_season_block(template, tmdb_id, number, numbers))
        )
        for number, numbers in seasons.items()
    }


async def _seed_show(
    session,
    *,
    tmdb_id: int | None,
    name: str = "Mirrored Show",
    synced: bool = True,
    credits_synced: bool = False,
) -> int:
    """A show the ingest mirrored — spine present, credits absent."""
    show_id = _next_id()
    stamp = datetime(2026, 8, 11, tzinfo=UTC)
    session.add(
        cm.Show(
            id=show_id,
            name=name,
            tmdb_id=tmdb_id,
            tmdb_synced_at=stamp if synced else None,
            credits_synced_at=stamp if credits_synced else None,
        )
    )
    await session.flush()
    await session.commit()
    return show_id


async def _seed_episode(session, show_id: int, *, season: int, number: int, tmdb_id: int) -> int:
    episode_id = _next_id()
    session.add(
        cm.Episode(
            id=episode_id,
            show_id=show_id,
            season_number=season,
            episode_number=number,
            name=f"S{season}E{number}",
            tmdb_id=tmdb_id,
        )
    )
    await session.flush()
    await session.commit()
    return episode_id


async def _mirror(session, tmdb_id: int, seasons: dict[int, list[int]], **kwargs) -> int:
    """A mirrored show plus a `catalog.episode` row for every episode upstream has."""
    show_id = await _seed_show(session, tmdb_id=tmdb_id, **kwargs)
    for season, numbers in seasons.items():
        for number in numbers:
            await _seed_episode(
                session,
                show_id,
                season=season,
                number=number,
                tmdb_id=_upstream_episode_id(tmdb_id, season, number),
            )
    return show_id


async def _run(session, **kwargs):
    async with TMDBClient(
        base_url=BASE,
        read_access_token="eyJ-not-a-real-token",
        rate_calls=200,
        rate_window=1,
        retry_base_delay=0.01,
    ) as client:
        return await backfill_credits(session, client, page_size=2, **kwargs)


async def _count(session, model, **filters) -> int:
    stmt = select(func.count()).select_from(model)
    for column, value in filters.items():
        stmt = stmt.where(getattr(model, column) == value)
    return (await session.execute(stmt)).scalar_one()


async def _credits_stamp(session, show_id: int):
    stmt = (
        select(cm.Show.credits_synced_at)
        .where(cm.Show.id == show_id)
        .execution_options(populate_existing=True)
    )
    return (await session.execute(stmt)).scalar_one()


# --- writing the credits ----------------------------------------------------


@respx.mock
async def test_writes_all_four_credit_tables_for_a_mirrored_show(session):
    """AC: a show carrying credits upstream has them in `catalog` afterwards."""
    show_id = await _mirror(session, 1396, {1: [1]})
    mock_series(1396, {1: [1]})

    result = await _run(session)

    assert await _count(session, cm.ShowCast, show_id=show_id) == 1
    assert await _count(session, cm.ShowCrew, show_id=show_id) == 1
    assert await _count(session, cm.EpisodeGuestCast) == 1
    assert await _count(session, cm.EpisodeCrew) == 1
    assert (result.shows_stamped, result.shows_without_credits) == (1, 0)


@respx.mock
async def test_a_season_outside_the_speculative_window_still_contributes_its_credits(session):
    """A season past the speculative window overflows `append_to_response` and
    needs its own request.

    The overflow is where a credits pass is most likely to go quietly wrong — the
    episodes are mirrored either way, so a show whose season 25 guest cast never
    arrived looks complete.
    """
    show_id = await _mirror(session, 1396, {1: [1], 25: [1]})
    season_routes = mock_series(1396, {1: [1], 25: [1]})

    await _run(session)

    assert season_routes[25].call_count == 1
    guests = await session.execute(
        select(func.count())
        .select_from(cm.EpisodeGuestCast)
        .join(cm.Episode, cm.Episode.id == cm.EpisodeGuestCast.episode_id)
        .where(cm.Episode.show_id == show_id, cm.Episode.season_number == 25)
    )
    assert guests.scalar_one() == 1


@respx.mock
async def test_a_show_with_no_credits_upstream_is_stamped_and_counted_apart(session):
    """AC: no credits upstream is a normal outcome, not a failure — and not a retry.

    The whole reason the watermark is a column: under a "has no `show_cast` row"
    work list this show would be re-fetched on every run forever, and the pass
    would never converge.
    """
    show_id = await _mirror(session, 1396, {1: [1]}, name="Obscure")
    mock_series(1396, {1: [1]}, cast=[], crew=[], guests=[], episode_crew=[])

    first = await _run(session)

    assert (first.shows_stamped, first.shows_without_credits) == (1, 1)
    assert await _credits_stamp(session, show_id) is not None

    second = await _run(session)
    assert second.shows_considered == 0


# --- what it must not write -------------------------------------------------


@respx.mock
async def test_writes_nothing_outside_the_credit_tables(session):
    """AC: `catalog.show`, `season` and `episode` are untouched but for the watermark.

    Proved the way the ticket asks for it — row counts and the spine's own
    columns either side of a run — because the failure this guards against is a
    pass that silently re-prunes a season or re-derives a runtime on a spine the
    app is serving reads from.
    """
    show_id = await _mirror(session, 1396, {1: [1]})
    # A season row the payload does not name. `upsert_series_payload` would prune
    # it; this pass must not, and must not insert TMDB's own seasons either.
    season_id = _next_id()
    session.add(cm.Season(id=season_id, show_id=show_id, season_number=7, name="Local Season 7"))
    await session.flush()
    await session.commit()

    before = {
        "shows": await _count(session, cm.Show),
        "seasons": await _count(session, cm.Season),
        "episodes": await _count(session, cm.Episode),
    }
    show_row = (
        await session.execute(
            select(cm.Show.name, cm.Show.overview, cm.Show.runtime, cm.Show.tmdb_synced_at)
            .where(cm.Show.id == show_id)
            .execution_options(populate_existing=True)
        )
    ).one()

    mock_series(1396, {1: [1]})
    await _run(session)

    after = {
        "shows": await _count(session, cm.Show),
        "seasons": await _count(session, cm.Season),
        "episodes": await _count(session, cm.Episode),
    }
    assert after == before
    assert (
        await session.execute(
            select(cm.Show.name, cm.Show.overview, cm.Show.runtime, cm.Show.tmdb_synced_at)
            .where(cm.Show.id == show_id)
            .execution_options(populate_existing=True)
        )
    ).one() == show_row


@respx.mock
async def test_an_episode_upstream_has_and_the_mirror_does_not_is_skipped(session):
    """A new episode is the delta's to mirror, not this pass's to insert.

    Writing the row here would make this a spine pass by the back door, and would
    do it for exactly the episodes the ingest has not yet seen.
    """
    show_id = await _mirror(session, 1396, {1: [1]})
    mock_series(1396, {1: [1, 2]})

    await _run(session)

    assert await _count(session, cm.Episode, show_id=show_id) == 1
    assert await _count(session, cm.EpisodeGuestCast) == 1


# --- the work list ----------------------------------------------------------


@respx.mock
async def test_a_show_the_ingest_never_reached_is_not_considered(session):
    """`tmdb_synced_at IS NULL` is the *ingest's* backlog, not this one's.

    A copied TV Maze row holds no TMDB spine to hang credits off, and fetching
    one here would spend the request on a show the ingest is going to fetch again
    anyway.
    """
    await _seed_show(session, tmdb_id=1396, synced=False)
    mock_series(1396, {1: [1]})

    result = await _run(session)

    assert result.shows_considered == 0


@respx.mock
async def test_a_locally_authored_show_is_not_considered(session):
    """No `tmdb_id` is no request to make (ADR-0008)."""
    await _seed_show(session, tmdb_id=None)

    result = await _run(session)

    assert result.shows_considered == 0


@respx.mock
async def test_a_second_run_over_the_same_shows_changes_nothing(session):
    """AC: idempotent."""
    show_id = await _mirror(session, 1396, {1: [1]})
    mock_series(1396, {1: [1]})
    await _run(session)
    cast_after_first = await _count(session, cm.ShowCast, show_id=show_id)

    second = await _run(session)

    assert second.shows_considered == 0
    assert await _count(session, cm.ShowCast, show_id=show_id) == cast_after_first


@respx.mock
async def test_an_interrupted_run_resumes_rather_than_restarting(session):
    """AC: the shows already written are not fetched again.

    `--limit` stands in for the interrupt: it stops the pass mid-backlog exactly
    the way a kill would, since the watermark is committed per show.
    """
    first_show = await _mirror(session, 1396, {1: [1]})
    second_show = await _mirror(session, 1397, {1: [1]})
    mock_series(1396, {1: [1]})
    mock_series(1397, {1: [1]})

    await _run(session, limit=1)
    assert await _credits_stamp(session, first_show) is not None
    assert await _credits_stamp(session, second_show) is None

    resumed = await _run(session)

    assert resumed.shows_considered == 1
    assert await _credits_stamp(session, second_show) is not None


# --- failures ---------------------------------------------------------------


@respx.mock
async def test_a_show_that_is_gone_upstream_is_stepped_over_and_left_unstamped(session):
    """NEU-1006: a deleted series is a data condition, not a broken upstream."""
    gone = await _seed_show(session, tmdb_id=1396)
    kept = await _mirror(session, 1397, {1: [1]})
    respx.get(f"{BASE}/tv/1396").mock(return_value=httpx.Response(404, json={}))
    mock_series(1397, {1: [1]})

    result = await _run(session)

    assert (result.shows_failed, result.shows_gone, result.shows_stamped) == (1, 1, 1)
    assert await _credits_stamp(session, gone) is None
    assert await _credits_stamp(session, kept) is not None


@respx.mock
async def test_enough_consecutive_real_failures_abort_the_pass(session):
    """A persistent upstream failure is not something to spend 228k requests on."""
    for _ in range(3):
        await _seed_show(session, tmdb_id=_next_id())
    respx.get(url__regex=rf"{BASE}/tv/\d+").mock(return_value=httpx.Response(500, json={}))

    with pytest.raises(CreditsBackfillAborted):
        await _run(session, failure_threshold=2)


@respx.mock
async def test_a_failed_show_leaves_no_partial_credits_behind(session):
    """The overflow request is the one that can fail after the show credits landed.

    Rolling back is what keeps the watermark's promise: a stamped show has a
    complete credit set, so a re-run never has to wonder whether it does.
    """
    show_id = await _mirror(session, 1396, {1: [1], 25: [1]})
    mock_series(1396, {1: [1], 25: [1]})
    respx.get(f"{BASE}/tv/1396/season/25").mock(return_value=httpx.Response(500, json={}))

    result = await _run(session)

    assert result.shows_stamped == 0
    assert await _credits_stamp(session, show_id) is None
    assert await _count(session, cm.ShowCast, show_id=show_id) == 0


# --- the report -------------------------------------------------------------


@respx.mock
async def test_report_counts_the_backlog_and_names_the_shows_users_track(session):
    """The artifact a person reads before spending 8.7 hours, and after."""
    tracked = await _mirror(session, 1396, {1: [1]}, name="Tracked")
    user = User(
        id=uuid.uuid4(),
        email=f"credits-{uuid.uuid4().hex[:8]}@example.com",
        password_hash="x",
        display_name="Watcher",
        handle=new_handle(),
    )
    session.add(user)
    await session.flush()
    session.add(UserShowWatch(user_id=user.id, show_id=tracked))
    await session.commit()

    before = await build_report(session)
    assert before.totals["shows_remaining"] >= 1
    assert before.user_touched_without_credits_total >= 1
    assert tracked in {row["show_id"] for row in before.user_touched_without_credits}

    mock_series(1396, {1: [1]})
    await _run(session)

    after = await build_report(session)
    assert after.table_counts["show_cast"] > before.table_counts["show_cast"]
    assert tracked not in {row["show_id"] for row in after.user_touched_without_credits}


@respx.mock
async def test_a_payload_without_the_credits_namespace_is_not_stamped(session):
    """The namespace is always appended, so its absence describes the response.

    Stamping here would retire the show having never seen its credits — the
    exact silent partial this ticket exists to repair one grain up, and the one
    failure mode the watermark cannot otherwise distinguish from success.
    """
    show_id = await _mirror(session, 1396, {1: [1]})
    template = _series_payload(1396, seasons={1: [1]})
    del template["aggregate_credits"]

    def _respond(request: httpx.Request) -> httpx.Response:
        payload = {k: v for k, v in template.items() if not k.startswith("_")}
        for key in request.url.params.get("append_to_response", "").split(","):
            if key == "season/1":
                payload[key] = _season_block(template, 1396, 1, [1])
        return httpx.Response(200, json=payload)

    respx.get(f"{BASE}/tv/1396").mock(side_effect=_respond)

    result = await _run(session)

    assert (result.shows_stamped, result.shows_failed, result.shows_gone) == (0, 1, 0)
    assert await _credits_stamp(session, show_id) is None
    assert await _count(session, cm.EpisodeGuestCast) == 0


@respx.mock
async def test_report_reaches_a_show_touched_only_through_an_episode_watch(session):
    """A show can carry watch history without a My Shows row.

    Counting only `user_show_watch` would leave that show out of the spot-check
    list entirely, which is the one way this report can be quietly narrower than
    it claims.
    """
    show_id = await _mirror(session, 1396, {1: [1]}, name="Watched Only")
    episode_id = (
        await session.execute(select(cm.Episode.id).where(cm.Episode.show_id == show_id).limit(1))
    ).scalar_one()
    user = User(
        id=uuid.uuid4(),
        email=f"credits-{uuid.uuid4().hex[:8]}@example.com",
        password_hash="x",
        display_name="Watcher",
        handle=new_handle(),
    )
    session.add(user)
    await session.flush()
    session.add(UserEpisodeWatch(user_id=user.id, episode_id=episode_id))
    await session.commit()

    report = await build_report(session)

    assert show_id in {row["show_id"] for row in report.user_touched_without_credits}
