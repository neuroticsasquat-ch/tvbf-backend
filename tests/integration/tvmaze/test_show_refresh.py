"""Pass A — the combined show refresh (NEU-926).

Two requests per show: `/shows/{id}?embed[]=seasons&embed[]=cast&embed[]=crew`
plus `/shows/{id}/episodes?specials=1`. Deliberately no `embed[]=episodes` —
the episodes endpoint already returns the full list including specials, so the
embed would be redundant payload over 87k shows.
"""

from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from tvbf.tvmaze import models as m
from tvbf.tvmaze import show_refresh as show_refresh_module
from tvbf.tvmaze.show_refresh import run_show_refresh


def show_payload(show_id: int, *, cast=None, crew=None, tvdb=None, rating=None) -> dict:
    return {
        "id": show_id,
        "name": f"Show {show_id}",
        "type": "Scripted",
        "updated": 1700000000,
        "genres": ["Drama"],
        "network": None,
        "webChannel": None,
        "externals": {"imdb": "tt1", "thetvdb": tvdb, "tvrage": None},
        "rating": {"average": rating},
        "_embedded": {
            "seasons": [{"id": show_id * 1000 + 1, "number": 1, "name": "S1"}],
            "cast": cast or [],
            "crew": crew or [],
        },
    }


def cast_entry(person_id: int, character_id: int, name: str) -> dict:
    return {
        "person": {"id": person_id, "name": name, "country": None, "image": None, "updated": 1},
        "character": {"id": character_id, "name": f"{name} the character", "image": None},
        "self": False,
        "voice": False,
    }


def crew_entry(person_id: int, name: str, type_: str) -> dict:
    return {
        "type": type_,
        "person": {"id": person_id, "name": name, "country": None, "updated": 1},
    }


class FakeClient:
    """Duck-types the three calls pass A makes, recording what it was asked for."""

    def __init__(
        self,
        shows: dict[int, dict],
        episodes: dict[int, list[dict]] | None = None,
        season_episodes: dict[int, list[dict]] | None = None,
    ):
        self._shows = shows
        self._episodes = episodes or {}
        self._season_episodes = season_episodes or {}
        self.show_calls: list[tuple[int, tuple[str, ...]]] = []
        self.episode_calls: list[tuple[int, bool]] = []
        self.season_calls: list[int] = []

    async def get_show(self, show_id: int, *, embed: list[str] | None = None) -> dict:
        self.show_calls.append((show_id, tuple(embed or ())))
        return self._shows[show_id]

    async def get_show_episodes(self, show_id: int, *, specials: bool = True) -> list[dict]:
        self.episode_calls.append((show_id, specials))
        return self._episodes.get(show_id, [])

    async def get_season_episodes(self, season_id: int) -> list[dict]:
        self.season_calls.append(season_id)
        return self._season_episodes.get(season_id, [])


def _http_error(show_id: int) -> httpx.HTTPStatusError:
    return httpx.HTTPStatusError(
        "boom",
        request=httpx.Request("GET", f"https://api.tvmaze.com/shows/{show_id}"),
        response=httpx.Response(500),
    )


@pytest.fixture
async def run_id(session):
    run = m.IngestRun(id=uuid4(), kind="show_refresh", status="running")
    session.add(run)
    await session.commit()
    return run.id


async def _refreshed_show(session, show_id: int) -> m.Show:
    return (
        await session.execute(
            select(m.Show).where(m.Show.id == show_id).execution_options(populate_existing=True)
        )
    ).scalar_one()


async def _refreshed_run(session, rid) -> m.IngestRun:
    return (
        await session.execute(
            select(m.IngestRun)
            .where(m.IngestRun.id == rid)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def test_refresh_processes_only_unsynced_shows(session, run_id):
    session.add_all(
        [
            m.Show(id=10, name="Done", tvmaze_updated=1, credits_synced_at=datetime.now(UTC)),
            m.Show(id=11, name="Todo", tvmaze_updated=1),
        ]
    )
    await session.commit()

    client = FakeClient({11: show_payload(11)})
    result = await run_show_refresh(session_factory=lambda: session, client=client, run_id=run_id)

    assert [sid for sid, _ in client.show_calls] == [11]
    assert result.shows_processed == 1
    assert result.shows_failed == 0


async def test_refresh_requests_credits_embeds_but_not_episodes(session, run_id):
    """The episodes endpoint returns everything, so embedding episodes would
    ship a redundant payload 87k times."""
    session.add(m.Show(id=12, name="A", tvmaze_updated=1))
    await session.commit()

    client = FakeClient({12: show_payload(12)})
    await run_show_refresh(session_factory=lambda: session, client=client, run_id=run_id)

    assert client.show_calls == [(12, ("seasons", "cast", "crew"))]
    assert client.episode_calls == [(12, True)]


async def test_refresh_writes_cast_crew_persons_characters_and_roles(session, run_id):
    session.add(m.Show(id=13, name="A", tvmaze_updated=1))
    await session.commit()

    client = FakeClient(
        {
            13: show_payload(
                13,
                cast=[cast_entry(100, 200, "Lead"), cast_entry(101, 201, "Second")],
                crew=[crew_entry(102, "Director", "Director")],
            )
        }
    )
    result = await run_show_refresh(session_factory=lambda: session, client=client, run_id=run_id)
    assert result.shows_processed == 1

    cast = (
        (
            await session.execute(
                select(m.ShowCast).where(m.ShowCast.show_id == 13).order_by(m.ShowCast.sort_order)
            )
        )
        .scalars()
        .all()
    )
    assert [c.person_id for c in cast] == [100, 101]  # upstream billing order

    crew = (await session.execute(select(m.ShowCrew).where(m.ShowCrew.show_id == 13))).scalars()
    assert [c.person_id for c in crew] == [102]

    persons = (await session.execute(select(m.Person))).scalars().all()
    assert {p.id for p in persons} == {100, 101, 102}

    characters = (await session.execute(select(m.Character))).scalars().all()
    assert {c.id for c in characters} == {200, 201}

    roles = (await session.execute(select(m.CrewRole))).scalars().all()
    assert {r.name for r in roles} == {"Director"}


async def test_refresh_writes_specials_from_the_episodes_endpoint(session, run_id):
    session.add(m.Show(id=14, name="A", tvmaze_updated=1))
    await session.commit()

    client = FakeClient(
        {14: show_payload(14)},
        episodes={
            14: [
                {"id": 500, "season": 1, "number": 1, "name": "E1"},
                {"id": 501, "season": 1, "number": None, "name": "Special", "airdate": ""},
            ]
        },
    )
    await run_show_refresh(session_factory=lambda: session, client=client, run_id=run_id)

    eps = (await session.execute(select(m.Episode).where(m.Episode.show_id == 14))).scalars().all()
    assert {e.id for e in eps} == {500, 501}
    special = next(e for e in eps if e.id == 501)
    assert special.number is None
    # The seasons embed is still requested, so specials resolve their season FK.
    assert special.season_id == 14001


async def test_refresh_recovers_tvdb_id_and_rating(session, run_id):
    """The two riders NEU-922 and NEU-161 hand to this pass."""
    session.add(m.Show(id=15, name="A", tvmaze_updated=1))
    await session.commit()

    client = FakeClient({15: show_payload(15, tvdb=264492, rating=8.5)})
    await run_show_refresh(session_factory=lambda: session, client=client, run_id=run_id)

    show = await _refreshed_show(session, 15)
    assert show.externals_tvdb == 264492
    assert show.rating_average == 8.5


async def test_refresh_stamps_both_watermarks(session, run_id):
    """`ratings_synced_at` is stamped too — free, and makes NEU-161's backfill a
    no-op whether or not it already finished in prod."""
    session.add(m.Show(id=16, name="A", tvmaze_updated=1))
    await session.commit()

    client = FakeClient({16: show_payload(16)})
    await run_show_refresh(session_factory=lambda: session, client=client, run_id=run_id)

    show = await _refreshed_show(session, 16)
    assert show.credits_synced_at is not None
    assert show.ratings_synced_at is not None


async def test_refresh_writes_episode_credits_for_the_shows_it_processes(session, run_id):
    """A show whose watermark is reset must re-fetch genuinely everything.

    Without the season step this pass would rewrite the show and its episodes
    and leave their credits behind — everything-but-credits, which is exactly
    the state a reset is meant to escape.
    """
    session.add(m.Show(id=19, name="A", tvmaze_updated=1))
    await session.commit()

    season_id = 19 * 1000 + 1
    client = FakeClient(
        {19: show_payload(19)},
        episodes={19: [{"id": 7000, "season": 1, "number": 1, "name": "E1"}]},
        season_episodes={
            season_id: [
                {
                    "id": 7000,
                    "season": 1,
                    "number": 1,
                    "name": "E1",
                    "_embedded": {
                        "guestcast": [],
                        "guestcrew": [
                            {
                                "guestCrewType": "Director",
                                "person": {"id": 8000, "name": "Ada", "updated": 1},
                            }
                        ],
                    },
                }
            ]
        },
    )
    await run_show_refresh(session_factory=lambda: session, client=client, run_id=run_id)

    assert client.season_calls == [season_id]

    crew = (await session.execute(select(m.EpisodeCrew))).scalars().all()
    assert [(c.episode_id, c.sort_order) for c in crew] == [(7000, 0)]

    season = (
        await session.execute(
            select(m.Season)
            .where(m.Season.id == season_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert season.credits_synced_at is not None


async def test_refresh_skips_the_season_step_when_the_show_write_failed(
    session, run_id, monkeypatch
):
    """No episode rows were committed, so there is nothing for credits to FK to."""
    session.add(m.Show(id=25, name="A", tvmaze_updated=1))
    await session.commit()

    async def boom(*args, **kwargs):
        raise RuntimeError("write failed")

    monkeypatch.setattr(show_refresh_module, "upsert_show_payload", boom)

    client = FakeClient({25: show_payload(25)})
    await run_show_refresh(session_factory=lambda: session, client=client, run_id=run_id)

    assert client.season_calls == []


async def test_rerun_is_a_no_op(session, run_id):
    session.add(m.Show(id=17, name="A", tvmaze_updated=1))
    await session.commit()

    client = FakeClient({17: show_payload(17)})
    await run_show_refresh(session_factory=lambda: session, client=client, run_id=run_id)
    assert len(client.show_calls) == 1

    second_run = m.IngestRun(id=uuid4(), kind="show_refresh", status="running")
    session.add(second_run)
    await session.commit()
    result = await run_show_refresh(
        session_factory=lambda: session, client=client, run_id=second_run.id
    )
    assert len(client.show_calls) == 1  # watermark respected
    assert result.shows_processed == 0


async def test_http_failure_on_the_show_call_is_non_fatal(session, run_id):
    session.add_all([m.Show(id=18, name="A", tvmaze_updated=1)])
    await session.commit()

    class FailingShow(FakeClient):
        async def get_show(self, show_id: int, *, embed: list[str] | None = None) -> dict:
            raise _http_error(show_id)

    result = await run_show_refresh(
        session_factory=lambda: session, client=FailingShow({}), run_id=run_id
    )
    assert result.shows_failed == 1
    assert result.shows_processed == 0
    assert (await _refreshed_show(session, 18)).credits_synced_at is None
    # Counted on the run row too, not just in memory — the status endpoint
    # reads the row, and that's the only progress signal a 27h run has.
    assert (await _refreshed_run(session, run_id)).shows_failed == 1


async def test_http_failure_on_the_episodes_call_is_non_fatal(session, run_id):
    """Unlike the ongoing path, a failed episodes fetch here fails the show —
    the watermark must not be stamped, or the special is lost for good."""
    session.add(m.Show(id=19, name="A", tvmaze_updated=1))
    await session.commit()

    class FailingEpisodes(FakeClient):
        async def get_show_episodes(self, show_id: int, *, specials: bool = True) -> list[dict]:
            raise _http_error(show_id)

    result = await run_show_refresh(
        session_factory=lambda: session,
        client=FailingEpisodes({19: show_payload(19)}),
        run_id=run_id,
    )
    assert result.shows_failed == 1
    assert result.shows_processed == 0
    assert (await _refreshed_show(session, 19)).credits_synced_at is None


async def test_write_failure_is_counted_and_leaves_the_show_unsynced(session, run_id, monkeypatch):
    """A show whose write transaction blew up must stay in the todo list."""
    session.add(m.Show(id=21, name="A", tvmaze_updated=1))
    await session.commit()

    async def boom(*args, **kwargs):
        raise RuntimeError("simulated write failure")

    monkeypatch.setattr("tvbf.tvmaze.show_refresh.upsert_show_cast", boom)

    client = FakeClient({21: show_payload(21, cast=[cast_entry(300, 400, "X")])})
    result = await run_show_refresh(session_factory=lambda: session, client=client, run_id=run_id)

    assert result.shows_failed == 1
    assert (await _refreshed_show(session, 21)).credits_synced_at is None


async def test_watermarks_roll_back_when_the_write_fails_after_stamping(
    session, run_id, monkeypatch
):
    """The real resumability guard: fail *after* both watermarks are stamped.

    A failure before `mark_credits_synced` proves nothing — the watermark was
    never written. This injects at `record_progress`, which runs after both
    stamps and before the commit, so only an actual transaction rollback can
    keep the show in the todo list. Without it a failed show is never retried
    and its cast is lost for the life of the mirror.
    """
    session.add(m.Show(id=22, name="A", tvmaze_updated=1))
    await session.commit()

    real_record_progress = show_refresh_module.record_progress

    async def boom_on_success(s, rid, processed_delta=0, failed_delta=0):
        # Only the success-path call raises; the failure arms still need to
        # record their increment, or the run itself would blow up.
        if processed_delta:
            raise RuntimeError("simulated failure after stamping")
        return await real_record_progress(
            s, rid, processed_delta=processed_delta, failed_delta=failed_delta
        )

    monkeypatch.setattr(show_refresh_module, "record_progress", boom_on_success)

    client = FakeClient({22: show_payload(22)})
    result = await run_show_refresh(session_factory=lambda: session, client=client, run_id=run_id)

    assert result.shows_failed == 1
    assert result.shows_processed == 0

    show = await _refreshed_show(session, 22)
    assert show.credits_synced_at is None
    assert show.ratings_synced_at is None


async def test_aborts_after_consecutive_failure_threshold(session, run_id):
    session.add_all(
        [
            m.Show(id=40, name="A", tvmaze_updated=1),
            m.Show(id=41, name="B", tvmaze_updated=1),
            m.Show(id=42, name="C", tvmaze_updated=1),
        ]
    )
    await session.commit()

    calls: list[int] = []

    class CountingFailingClient(FakeClient):
        async def get_show(self, show_id: int, *, embed: list[str] | None = None) -> dict:
            calls.append(show_id)
            raise _http_error(show_id)

    result = await run_show_refresh(
        session_factory=lambda: session,
        client=CountingFailingClient({}),
        run_id=run_id,
        failure_threshold=2,
    )
    assert result.shows_failed == 2
    assert calls == [40, 41]  # aborted before the third

    refreshed = (
        await session.execute(
            select(m.IngestRun)
            .where(m.IngestRun.id == run_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert refreshed.status == "failed"
    assert refreshed.error is not None


async def test_successful_run_is_finalized_as_succeeded(session, run_id):
    session.add(m.Show(id=50, name="A", tvmaze_updated=1))
    await session.commit()

    client = FakeClient({50: show_payload(50)})
    await run_show_refresh(session_factory=lambda: session, client=client, run_id=run_id)

    refreshed = (
        await session.execute(
            select(m.IngestRun)
            .where(m.IngestRun.id == run_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert refreshed.status == "succeeded"
