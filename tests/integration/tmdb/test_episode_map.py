"""Episode-grain mapping and its unmatched report (NEU-1045).

Every test here is one of the ticket's acceptance criteria or one of the ways an
episode could silently take an id belonging to a different episode — which at
this grain means re-labelling somebody's watch history with nothing downstream
to catch it.

Seeding is doubled for the same reason `test_human_queue.py` doubles it:
`app.user_episode_watch` carries a foreign key into `tvmaze.episode`, while the
rows being mapped live in `catalog`. The two share an id, which is the
migration's premise (NEU-1042 preserved TV Maze ids as the catalog surrogates)
and the only reason one query can span both.

The series route is mocked the way the live API was measured to behave — an
appended `season/N` a show does not have is dropped silently — so the
speculate-then-reconcile path `fetch_series_with_seasons` owns is exercised
rather than assumed.
"""

from datetime import date

import httpx
import respx
from sqlalchemy import func, select

from tests.fixtures.spines import without_catalog_fk
from tests.fixtures.tmdb.series_factory import (
    make_episode,
    make_season_detail,
    make_season_summary,
    make_series,
)
from tvbf.app.models import UserEpisodeRating, UserEpisodeWatch
from tvbf.catalog import models as cm
from tvbf.tmdb.client import TMDBClient
from tvbf.tmdb.episode_map import (
    EpisodeMapAborted,
    build_report,
    map_episode_ids,
)

BASE = "https://api.themoviedb.org/3"

# Well clear of the browse fixtures' catalog, so these rows never collide with a
# seeded show and every assertion can name exact ids.
_ID = 9_600_000


def _next_id() -> int:
    global _ID
    _ID += 1
    return _ID


def _series_payload(tmdb_id: int, seasons: dict[int, list[int]]) -> dict:
    """A `/tv/{id}` body naming `{season_number: [episode numbers]}`, no appends."""
    payload = make_series(tmdb_id, seasons=0, append_seasons=False)
    payload["seasons"] = [
        make_season_summary(tmdb_id * 100 + number, number, episode_count=len(numbers))
        for number, numbers in sorted(seasons.items())
    ]
    return payload


def _season_block(tmdb_id: int, number: int, episode_numbers: list[int]) -> dict:
    return make_season_detail(
        number,
        [make_episode(tmdb_id * 10_000 + number * 100 + e, number, e) for e in episode_numbers],
    )


def mock_series(tmdb_id: int, seasons: dict[int, list[int]]) -> dict[int, respx.Route]:
    """Route `/tv/{id}` and every `/tv/{id}/season/{n}` for one show.

    The series route honours `append_to_response` exactly as measured: a
    `season/N` block comes back only for a season the show has *and* the caller
    asked for. Seasons outside the speculative window therefore have to be
    fetched standalone, which is what the season routes are for.
    """

    def _respond(request: httpx.Request) -> httpx.Response:
        payload = _series_payload(tmdb_id, seasons)
        for key in request.url.params.get("append_to_response", "").split(","):
            if not key.startswith("season/"):
                continue
            number = int(key.removeprefix("season/"))
            if number in seasons:
                payload[key] = _season_block(tmdb_id, number, seasons[number])
        return httpx.Response(200, json=payload)

    respx.get(f"{BASE}/tv/{tmdb_id}").mock(side_effect=_respond)
    return {
        number: respx.get(f"{BASE}/tv/{tmdb_id}/season/{number}").mock(
            return_value=httpx.Response(200, json=_season_block(tmdb_id, number, numbers))
        )
        for number, numbers in seasons.items()
    }


async def _seed_show(session, *, tmdb_id: int | None, name: str = "Mapped Show") -> int:
    """One show, in both spines, sharing an id."""
    show_id = _next_id()
    session.add(cm.Show(id=show_id, name=name, tmdb_id=tmdb_id))
    await session.flush()
    await session.commit()
    return show_id


async def _seed_episode(
    session,
    show_id: int,
    *,
    season: int,
    number: int,
    tmdb_id: int | None = None,
    name: str = "An Episode",
) -> int:
    """A copied episode: the `tvmaze` row user data points at, and its catalog twin."""
    episode_id = _next_id()
    session.add(
        cm.Episode(
            id=episode_id,
            show_id=show_id,
            season_number=season,
            episode_number=number,
            name=name,
            tmdb_id=tmdb_id,
            air_date=date(2008, 1, 20),
        )
    )
    await session.flush()
    await session.commit()
    return episode_id


async def _run(session, **kwargs):
    async with TMDBClient(
        base_url=BASE,
        read_access_token="eyJ-not-a-real-token",
        rate_calls=200,
        rate_window=1,
        retry_base_delay=0.01,
    ) as client:
        return await map_episode_ids(session, client, batch_size=2, **kwargs)


async def _episode_tmdb_id(session, episode_id: int) -> int | None:
    stmt = (
        select(cm.Episode.tmdb_id)
        .where(cm.Episode.id == episode_id)
        .execution_options(populate_existing=True)
    )
    return (await session.execute(stmt)).scalar_one()


# --- matching ---------------------------------------------------------------


@respx.mock
async def test_an_episode_matching_on_season_and_number_is_stamped(session):
    show_id = await _seed_show(session, tmdb_id=1396)
    episode_id = await _seed_episode(session, show_id, season=1, number=2)
    mock_series(1396, {1: [1, 2]})

    result = await _run(session)

    assert await _episode_tmdb_id(session, episode_id) == 1396 * 10_000 + 102
    assert result.episodes.matched == 1


@respx.mock
async def test_an_episode_upstream_does_not_have_is_left_alone(session):
    """A split, a merge or a renumbering — unmatched is a valid outcome, not a failure."""
    show_id = await _seed_show(session, tmdb_id=1396)
    episode_id = await _seed_episode(session, show_id, season=1, number=9)
    mock_series(1396, {1: [1, 2]})

    result = await _run(session)

    assert await _episode_tmdb_id(session, episode_id) is None
    assert (result.episodes.matched, result.episodes.unmatched) == (0, 1)


@respx.mock
async def test_a_season_outside_the_speculative_window_still_maps(session):
    """A season past the window overflows `append_to_response` and needs its own request.

    The window is `0..19` here rather than the ingest's narrower one: this pass appends
    no namespaces, so the whole 20-entry budget goes to seasons.
    """
    show_id = await _seed_show(session, tmdb_id=1396)
    episode_id = await _seed_episode(session, show_id, season=25, number=1)
    mock_series(1396, {1: [1], 25: [1]})

    result = await _run(session)

    assert await _episode_tmdb_id(session, episode_id) == 1396 * 10_000 + 2501
    assert result.episodes.matched == 1


@respx.mock
async def test_a_twelfth_season_rides_the_first_request(session):
    """The budget the ingest spends on namespaces is spent on seasons here."""
    show_id = await _seed_show(session, tmdb_id=1396)
    await _seed_episode(session, show_id, season=12, number=1)
    season_routes = mock_series(1396, {12: [1]})

    await _run(session)

    assert season_routes[12].call_count == 0


@respx.mock
async def test_a_synthetic_special_is_never_mapped_and_never_retried(session):
    """The copy's negative numbers have no upstream counterpart by construction.

    They are counted apart from `unmatched` and, more importantly, they do not
    keep the show in the work list — otherwise every run would re-fetch the
    27,498 specials in production forever.
    """
    show_id = await _seed_show(session, tmdb_id=1396)
    special = await _seed_episode(session, show_id, season=1, number=-1)
    await _seed_episode(session, show_id, season=1, number=1)
    mock_series(1396, {0: [1], 1: [1]})

    first = await _run(session)
    second = await _run(session)

    assert await _episode_tmdb_id(session, special) is None
    assert (first.episodes.matched, first.episodes.synthetic) == (1, 1)
    assert second.shows_considered == 0


@respx.mock
async def test_two_rows_sharing_a_season_and_number_are_both_left_unmapped(session):
    """TV Maze's duplicate `(show, season, number)` triples resolve to unmatched.

    Which of the two the upstream episode *is* has no answer, and picking one
    would attach a real id to a row on the strength of its primary key.
    """
    show_id = await _seed_show(session, tmdb_id=1396)
    first = await _seed_episode(session, show_id, season=1, number=1)
    second = await _seed_episode(session, show_id, season=1, number=1)
    mock_series(1396, {1: [1]})

    result = await _run(session)

    assert await _episode_tmdb_id(session, first) is None
    assert await _episode_tmdb_id(session, second) is None
    assert (result.episodes.ambiguous, result.episodes.matched) == (2, 0)


@respx.mock
async def test_an_id_another_row_already_holds_is_a_collision_not_a_crash(session):
    """Post-ingest this is every episode of a copied show; it must not raise."""
    show_id = await _seed_show(session, tmdb_id=1396)
    taken = 1396 * 10_000 + 101
    await _seed_episode(session, show_id, season=99, number=1, tmdb_id=taken)
    duplicate = await _seed_episode(session, show_id, season=1, number=1)
    mock_series(1396, {1: [1]})

    result = await _run(session)

    assert await _episode_tmdb_id(session, duplicate) is None
    assert (result.episodes.collisions, result.episodes.matched) == (1, 0)


@respx.mock
async def test_a_rerun_neither_rewrites_nor_reconsiders_a_mapped_show(session):
    show_id = await _seed_show(session, tmdb_id=1396)
    episode_id = await _seed_episode(session, show_id, season=1, number=1)
    mock_series(1396, {1: [1]})
    await _run(session)

    second = await _run(session)

    assert await _episode_tmdb_id(session, episode_id) == 1396 * 10_000 + 101
    assert second.shows_considered == 0


@respx.mock
async def test_an_unmatched_show_is_never_fetched(session):
    """No `tmdb_id` means nothing to fetch — the show grain gates the episode grain."""
    show_id = await _seed_show(session, tmdb_id=None)
    await _seed_episode(session, show_id, season=1, number=1)
    route = respx.get(url__regex=rf"{BASE}/tv/.*").mock(return_value=httpx.Response(200, json={}))

    result = await _run(session)

    assert result.shows_considered == 0
    assert route.call_count == 0


@respx.mock
async def test_limit_caps_how_many_shows_are_considered(session):
    first = await _seed_show(session, tmdb_id=1396)
    await _seed_episode(session, first, season=1, number=1)
    second = await _seed_show(session, tmdb_id=1397)
    later = await _seed_episode(session, second, season=1, number=1)
    mock_series(1396, {1: [1]})
    mock_series(1397, {1: [1]})

    result = await _run(session, limit=1)

    assert result.shows_considered == 1
    assert await _episode_tmdb_id(session, later) is None


# --- failures ----------------------------------------------------------------


@respx.mock
async def test_a_series_gone_upstream_leaves_the_show_unmapped(session):
    """A 404 is a data condition, not a broken upstream — the pass carries on."""
    gone = await _seed_show(session, tmdb_id=1396)
    await _seed_episode(session, gone, season=1, number=1)
    survivor = await _seed_show(session, tmdb_id=1397)
    episode_id = await _seed_episode(session, survivor, season=1, number=1)
    respx.get(f"{BASE}/tv/1396").mock(return_value=httpx.Response(404, json={}))
    mock_series(1397, {1: [1]})

    result = await _run(session)

    assert (result.shows_failed, result.shows_gone) == (1, 1)
    assert await _episode_tmdb_id(session, episode_id) == 1397 * 10_000 + 101


@respx.mock
async def test_consecutive_failures_abort_the_pass_without_losing_earlier_work(session):
    mapped = await _seed_show(session, tmdb_id=1396)
    episode_id = await _seed_episode(session, mapped, season=1, number=1)
    mock_series(1396, {1: [1]})
    for tmdb_id in (1397, 1398):
        broken = await _seed_show(session, tmdb_id=tmdb_id)
        await _seed_episode(session, broken, season=1, number=1)
        respx.get(f"{BASE}/tv/{tmdb_id}").mock(return_value=httpx.Response(400, json={}))

    try:
        await _run(session, failure_threshold=2)
    except EpisodeMapAborted:
        pass
    else:  # pragma: no cover - the assertion belongs to the failure path
        raise AssertionError("the pass should have aborted")

    assert await _episode_tmdb_id(session, episode_id) == 1396 * 10_000 + 101


# --- the report ---------------------------------------------------------------


@respx.mock
async def test_every_unmapped_watched_episode_is_in_the_report(session, make_user):
    """The ticket's first acceptance criterion, stated as itself."""
    user = await make_user(email="em1@example.com")
    show_id = await _seed_show(session, tmdb_id=1396, name="Renumbered")
    watched = await _seed_episode(session, show_id, season=1, number=9, name="The Odd One")
    mapped = await _seed_episode(session, show_id, season=1, number=1)
    session.add(UserEpisodeWatch(user_id=user.id, episode_id=watched))
    session.add(UserEpisodeWatch(user_id=user.id, episode_id=mapped))
    await session.commit()
    mock_series(1396, {1: [1]})
    await _run(session)

    report = await build_report(session)

    rows = [row for row in report.unmatched_user_data if row["episode_id"] == watched]
    assert rows == [
        {
            "episode_id": watched,
            "show_id": show_id,
            "show_name": "Renumbered",
            "show_tmdb_id": 1396,
            "match_method": None,
            "season_number": 1,
            "episode_number": 9,
            "episode_name": "The Odd One",
            "air_date": "2008-01-20",
            "synthetic": False,
            "watches": 1,
            "ratings": 0,
        }
    ]
    assert not [row for row in report.unmatched_user_data if row["episode_id"] == mapped]


@respx.mock
async def test_the_report_leads_with_the_most_watched_row(session, make_user):
    """Watch count is the only column that says what being wrong would cost."""
    one = await make_user(email="em2@example.com")
    two = await make_user(email="em3@example.com")
    show_id = await _seed_show(session, tmdb_id=1396)
    quiet = await _seed_episode(session, show_id, season=1, number=8)
    popular = await _seed_episode(session, show_id, season=1, number=9)
    session.add(UserEpisodeWatch(user_id=one.id, episode_id=quiet))
    session.add(UserEpisodeWatch(user_id=one.id, episode_id=popular))
    session.add(UserEpisodeWatch(user_id=two.id, episode_id=popular))
    await session.commit()
    mock_series(1396, {1: [1]})
    await _run(session)

    report = await build_report(session)

    ours = [row for row in report.unmatched_user_data if row["show_id"] == show_id]
    assert [(row["episode_id"], row["watches"]) for row in ours] == [(popular, 2), (quiet, 1)]


@respx.mock
async def test_an_unmapped_episode_nobody_watched_is_not_in_the_report(session):
    """Noise: the residue is tens of thousands of rows and only some of it matters."""
    show_id = await _seed_show(session, tmdb_id=1396)
    ignored = await _seed_episode(session, show_id, season=1, number=9)
    mock_series(1396, {1: [1]})
    await _run(session)

    report = await build_report(session)

    assert not [row for row in report.unmatched_user_data if row["episode_id"] == ignored]
    assert report.totals["episodes_unmapped"] >= 1


@respx.mock
async def test_a_rated_episode_carries_user_data_too(session, make_user):
    user = await make_user(email="em4@example.com")
    show_id = await _seed_show(session, tmdb_id=1396)
    rated = await _seed_episode(session, show_id, season=1, number=9)
    session.add(UserEpisodeRating(user_id=user.id, episode_id=rated, stars=4.0))
    await session.commit()
    mock_series(1396, {1: [1]})
    await _run(session)

    report = await build_report(session)

    (row,) = [r for r in report.unmatched_user_data if r["episode_id"] == rated]
    assert (row["watches"], row["ratings"]) == (0, 1)


@respx.mock
async def test_a_show_that_mapped_nothing_is_flagged_apart_from_scattered_misses(session):
    """The ticket's third criterion: 0% is a signal about the *show*, not an episode."""
    systematic = await _seed_show(session, tmdb_id=1396, name="Wrong Series")
    await _seed_episode(session, systematic, season=1, number=1)
    await _seed_episode(session, systematic, season=1, number=2)
    mock_series(1396, {7: [1, 2]})

    scattered = await _seed_show(session, tmdb_id=1397, name="Mostly Fine")
    await _seed_episode(session, scattered, season=1, number=1)
    await _seed_episode(session, scattered, season=1, number=9)
    mock_series(1397, {1: [1]})

    await _run(session)
    report = await build_report(session)

    flagged = {row["show_id"] for row in report.systematic_shows}
    assert systematic in flagged
    assert scattered not in flagged


@respx.mock
async def test_a_show_whose_only_residue_is_specials_is_not_flagged(session):
    """A synthetic special cannot map, so a show full of them is not a failed match."""
    show_id = await _seed_show(session, tmdb_id=1396)
    await _seed_episode(session, show_id, season=1, number=1)
    await _seed_episode(session, show_id, season=1, number=-1)
    mock_series(1396, {1: [1]})

    await _run(session)
    report = await build_report(session)

    assert show_id not in {row["show_id"] for row in report.systematic_shows}


@respx.mock
async def test_a_watched_special_is_reported_as_synthetic(session, make_user):
    """156 watched specials in production, and none of them is a mapping failure.

    They belong in the report — a watched episode is never silently dropped —
    but a reader must not have to know that a negative number means invented.
    """
    user = await make_user(email="em6@example.com")
    show_id = await _seed_show(session, tmdb_id=1396)
    special = await _seed_episode(session, show_id, season=1, number=-1)
    session.add(UserEpisodeWatch(user_id=user.id, episode_id=special))
    await session.commit()
    mock_series(1396, {0: [1], 1: [1]})
    await _run(session)

    report = await build_report(session)

    (row,) = [r for r in report.unmatched_user_data if r["episode_id"] == special]
    assert row["synthetic"] is True


async def test_a_watch_the_copy_never_mirrored_is_reported_loudly(session, make_user):
    """A watch whose episode has no `catalog.episode` row at all.

    Such a row is invisible to every other query here, all of which read *from*
    `catalog.episode` — so without this it would read as a clean report while a
    user's watch had no catalog row at all. The state arose before cutover, when
    the TV Maze daily kept adding watchable episodes after `copy:catalog` had
    run; since NEU-1051 there is one spine and the foreign key forbids it, which
    is why the constraint has to come down to reconstruct it here.
    """
    user = await make_user(email="em7@example.com")
    episode_id = _next_id()
    await session.flush()
    async with without_catalog_fk(session, "user_episode_watch"):
        session.add(UserEpisodeWatch(user_id=user.id, episode_id=episode_id))
        await session.commit()

        report = await build_report(session)

    assert {"episode_id": episode_id, "watches": 1} in report.unmirrored_watches
    assert report.totals["unmirrored_watched_episodes"] >= 1


@respx.mock
async def test_the_pass_modifies_no_user_episode_watch_row(session, make_user):
    """The ticket's second criterion. Nothing below `app` is written at all."""
    user = await make_user(email="em5@example.com")
    show_id = await _seed_show(session, tmdb_id=1396)
    episode_id = await _seed_episode(session, show_id, season=1, number=1)
    session.add(UserEpisodeWatch(user_id=user.id, episode_id=episode_id))
    await session.commit()
    before = (
        await session.execute(select(func.count()).select_from(UserEpisodeWatch))
    ).scalar_one()
    mock_series(1396, {1: [1]})

    await _run(session)

    watches = (
        (
            await session.execute(
                select(UserEpisodeWatch.episode_id).where(UserEpisodeWatch.user_id == user.id)
            )
        )
        .scalars()
        .all()
    )
    assert watches == [episode_id]
    after = (await session.execute(select(func.count()).select_from(UserEpisodeWatch))).scalar_one()
    assert after == before
