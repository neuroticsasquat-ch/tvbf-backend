"""The cached oracle link: the three states, the expiry, and the stale id (NEU-1148).

Driven directly rather than through a full pass, which is the point of the seam
— the invalidate-and-retry path is three branches deep and a test reaching it
through `run_airdate_reconcile` would be testing the trust rule by accident.

**The wire is mocked, not the client.** Request counts are the whole subject
here, and a hand-rolled stub counts calls to itself rather than requests; with
`respx` the counts asserted below are the ones TV Maze would have seen. The real
`ShowToCheck` and `ReconcileResult` are used for the same reason — they are what
the pass passes, so the structural types `show_state` declares are checked
against the things that actually satisfy them.
"""

from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
import respx
from sqlalchemy import select

from tvbf.airdates.client import TVMazeOracleClient
from tvbf.airdates.reconcile import ReconcileResult, ShowToCheck
from tvbf.airdates.show_state import RELOOKUP_MISSING_AFTER, oracle_episodes
from tvbf.catalog import models as m
from tvbf.rate_budget import RateLimiter

BASE = "https://api.tvmaze.com"

SHOW = ShowToCheck(show_id=900, name="Show 900", imdb_id="tt900", tvdb_id=None)


def _client() -> TVMazeOracleClient:
    # An isolated in-process limiter: this test is about requests, not pacing.
    return TVMazeOracleClient(
        base_url=BASE, rate_calls=18, rate_window=10, limiter=RateLimiter(18, 10)
    )


def _lookup(*, tvmaze_id: int | None):
    """`/lookup/shows` as measured: `301` with the id in `Location`, or `404`."""
    return respx.get(f"{BASE}/lookup/shows").mock(
        return_value=httpx.Response(404)
        if tvmaze_id is None
        else httpx.Response(301, headers={"Location": f"{BASE}/shows/{tvmaze_id}"})
    )


def _episodes_route(tvmaze_id: int, *, carried: bool = True, episodes: int = 1):
    """`/shows/{id}/episodes`. `carried=False` is the 404 a stale link gets."""
    if not carried:
        return respx.get(f"{BASE}/shows/{tvmaze_id}/episodes").mock(
            return_value=httpx.Response(404)
        )
    return respx.get(f"{BASE}/shows/{tvmaze_id}/episodes").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"season": 1, "number": n, "airdate": f"2020-01-0{n}"}
                for n in range(1, episodes + 1)
            ],
        )
    )


@pytest.fixture
async def show(session):
    session.add(m.Show(id=900, tmdb_id=900, name="Show 900", imdb_id="tt900"))
    await session.commit()
    return SHOW


async def _link(session, show_id: int) -> tuple[int | None, datetime] | None:
    row = (
        await session.execute(
            select(m.AirdateShowState.tvmaze_id, m.AirdateShowState.resolved_at)
            .where(m.AirdateShowState.show_id == show_id)
            .execution_options(populate_existing=True)
        )
    ).one_or_none()
    return None if row is None else (row.tvmaze_id, row.resolved_at)


async def _seed_link(session, show_id: int, tvmaze_id: int | None, *, age: timedelta) -> None:
    session.add(
        m.AirdateShowState(
            show_id=show_id, tvmaze_id=tvmaze_id, resolved_at=datetime.now(UTC) - age
        )
    )
    await session.commit()


class TestTheThreeStates:
    @respx.mock
    async def test_a_show_never_asked_about_is_looked_up_and_recorded(self, session, show):
        lookup, episodes = _lookup(tvmaze_id=5), _episodes_route(5)
        result = ReconcileResult()

        async with _client() as client:
            found = await oracle_episodes(session, client, show, result)
        await session.commit()

        assert found is not None and [e.airdate for e in found] == [date(2020, 1, 1)]
        assert (lookup.call_count, episodes.call_count) == (1, 1)
        assert (result.lookups_spent, result.links_reused) == (1, 0)
        assert await _link(session, 900) == (
            5,
            pytest.approx(datetime.now(UTC), abs=timedelta(minutes=1)),
        )

    @respx.mock
    async def test_a_resolved_link_costs_no_lookup(self, session, show):
        """AC 1, at the grain that decides it: one TV Maze request per show in
        steady state, and the one that goes is the re-derivation."""
        await _seed_link(session, 900, 5, age=timedelta(days=1))
        lookup, episodes = _lookup(tvmaze_id=5), _episodes_route(5)
        result = ReconcileResult()

        async with _client() as client:
            found = await oracle_episodes(session, client, show, result)

        assert found is not None and len(found) == 1
        assert (lookup.call_count, episodes.call_count) == (0, 1)
        assert (result.lookups_spent, result.links_reused) == (0, 1)

    @respx.mock
    async def test_a_negative_is_recorded_rather_than_left_absent(self, session, show):
        """The row is what distinguishes *asked, no counterpart* from *never
        asked*; without it the ~500 shows TV Maze has never heard of would be
        re-looked-up every night forever."""
        _lookup(tvmaze_id=None)
        result = ReconcileResult()

        async with _client() as client:
            assert await oracle_episodes(session, client, show, result) is None
        await session.commit()

        link = await _link(session, 900)
        assert link is not None
        tvmaze_id, resolved_at = link
        assert tvmaze_id is None
        assert datetime.now(UTC) - resolved_at < timedelta(minutes=1)

    @respx.mock
    async def test_a_fresh_negative_is_reused_without_a_request(self, session, show):
        await _seed_link(session, 900, None, age=timedelta(days=1))
        lookup, episodes = _lookup(tvmaze_id=5), _episodes_route(5)
        result = ReconcileResult()

        async with _client() as client:
            assert await oracle_episodes(session, client, show, result) is None

        assert (lookup.call_count, episodes.call_count) == (0, 0)
        assert result.links_reused == 1

    @respx.mock
    async def test_a_show_the_oracle_carries_with_no_episodes_is_empty_not_none(
        self, session, show
    ):
        """`[]` and `None` mean different things all the way up: an empty list is
        a show to compare and find nothing in, `None` is a show to report as
        having no counterpart."""
        _lookup(tvmaze_id=5)
        _episodes_route(5, episodes=0)

        async with _client() as client:
            assert await oracle_episodes(session, client, show, ReconcileResult()) == []


class TestExpiry:
    @respx.mock
    async def test_a_stale_negative_is_asked_about_again(self, session, show):
        """AC 2's second half. A negative is not permanent — a show TV Maze adds
        later should eventually be found."""
        await _seed_link(session, 900, None, age=RELOOKUP_MISSING_AFTER + timedelta(days=1))
        lookup = _lookup(tvmaze_id=5)
        _episodes_route(5)
        result = ReconcileResult()

        async with _client() as client:
            found = await oracle_episodes(session, client, show, result)
        await session.commit()

        assert found is not None and len(found) == 1
        assert (lookup.call_count, result.lookups_spent, result.links_reused) == (1, 1, 0)
        link = await _link(session, 900)
        assert link is not None and link[0] == 5

    @respx.mock
    async def test_the_interval_is_the_one_thing_a_caller_may_override(self, session, show):
        """A module constant beside `MAX_OFFSET_DAYS`, not a setting — so a test
        passes an interval rather than waiting thirty days."""
        await _seed_link(session, 900, None, age=timedelta(minutes=5))
        lookup = _lookup(tvmaze_id=5)
        _episodes_route(5, episodes=0)

        async with _client() as client:
            found = await oracle_episodes(
                session,
                client,
                show,
                ReconcileResult(),
                relookup_missing_after=timedelta(minutes=1),
            )

        assert found == []
        assert lookup.call_count == 1

    @respx.mock
    async def test_a_resolved_id_is_never_re_looked_up_on_a_timer(self, session, show):
        """The asymmetry is the design: expiring resolved ids would spend back
        exactly the requests the cache exists to save. A link that stops working
        is handled by invalidation instead."""
        await _seed_link(session, 900, 5, age=timedelta(days=3650))
        lookup = _lookup(tvmaze_id=7)
        _episodes_route(5, episodes=0)

        async with _client() as client:
            await oracle_episodes(
                session,
                client,
                show,
                ReconcileResult(),
                relookup_missing_after=timedelta(seconds=1),
            )

        assert lookup.call_count == 0


class TestAStaleLink:
    @respx.mock
    async def test_an_id_that_stops_resolving_is_re_looked_up_and_replaced(self, session, show):
        """AC 3. Three requests for that show that night, one thereafter — where
        doing nothing would end the show's reconciliation forever, visible only
        as one more season nothing could be compared for."""
        await _seed_link(session, 900, 5, age=timedelta(days=1))
        gone = _episodes_route(5, carried=False)
        lookup = _lookup(tvmaze_id=7)
        moved = _episodes_route(7)
        result = ReconcileResult()

        async with _client() as client:
            found = await oracle_episodes(session, client, show, result)
        await session.commit()

        assert found is not None and len(found) == 1
        assert (gone.call_count, lookup.call_count, moved.call_count) == (1, 1, 1)
        assert (result.links_invalidated, result.lookups_spent) == (1, 1)
        # `links_reused` means "resolved from a cached row, no request". This
        # show spent two, so it is not one of them.
        assert result.links_reused == 0
        link = await _link(session, 900)
        assert link is not None and link[0] == 7

    @respx.mock
    async def test_a_show_that_has_left_tv_maze_entirely_becomes_a_negative(self, session, show):
        """The re-lookup finds nothing, so the row flips to the negative and the
        show is reported as having no counterpart — the same conclusion, and the
        same log line, as a show that never resolved."""
        await _seed_link(session, 900, 5, age=timedelta(days=1))
        _episodes_route(5, carried=False)
        _lookup(tvmaze_id=None)
        result = ReconcileResult()

        async with _client() as client:
            assert await oracle_episodes(session, client, show, result) is None
        await session.commit()

        assert result.links_invalidated == 1
        link = await _link(session, 900)
        assert link is not None and link[0] is None

    @respx.mock
    async def test_an_id_that_resolves_but_serves_nothing_is_stored_as_a_negative(
        self, session, show
    ):
        """A show whose re-resolved id also 404s costs three requests that night
        and **one a month** thereafter. Storing the id instead would bank a link
        every night's first request proves dead — and only negatives expire, so
        the three-request dance would repeat forever, which is worse than the
        two requests the cache replaced."""
        await _seed_link(session, 900, 5, age=timedelta(days=1))
        gone = _episodes_route(5, carried=False)
        lookup = _lookup(tvmaze_id=7)
        also_gone = _episodes_route(7, carried=False)
        result = ReconcileResult()

        async with _client() as client:
            assert await oracle_episodes(session, client, show, result) is None
        await session.commit()

        assert (gone.call_count, lookup.call_count, also_gone.call_count) == (1, 1, 1)
        link = await _link(session, 900)
        assert link is not None and link[0] is None
