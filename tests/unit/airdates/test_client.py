"""The TV Maze oracle client's HTTP layer (NEU-1145).

**This file exists because its absence cost a production run.** Every other test
of the airdate pass stubs the client, which is right for testing the trust rule
— and meant nothing exercised the wire. `/lookup/shows` answers `301` with the
id in `Location`, httpx does not follow redirects by default, and so the first
prod run failed all ten of its first ten shows and aborted on the
consecutive-failure threshold. The endpoint's real shapes are pinned here.
"""

from datetime import date

import httpx
import pytest
import respx

from tvbf.airdates.client import TVMazeOracleClient
from tvbf.rate_budget import RateLimiter

BASE = "https://api.tvmaze.com"


def _client(**overrides) -> TVMazeOracleClient:
    # An isolated in-process limiter, so no unit test needs a database.
    return TVMazeOracleClient(
        base_url=BASE,
        rate_calls=18,
        rate_window=10,
        limiter=RateLimiter(18, 10),
        **overrides,
    )


class TestLookup:
    @respx.mock
    async def test_a_redirect_carries_the_id_in_its_location_header(self):
        """The live shape, measured 2026-08-14. Reading the header rather than
        following the redirect is what keeps a lookup at one request: httpx
        would follow internally, spending a request the limiter never sees."""
        route = respx.get(f"{BASE}/lookup/shows", params={"imdb": "tt2356777"}).mock(
            return_value=httpx.Response(301, headers={"Location": f"{BASE}/shows/5"})
        )

        async with _client() as client:
            assert await client.lookup_show(imdb_id="tt2356777", tvdb_id=None) == 5
        assert route.call_count == 1

    @respx.mock
    async def test_a_body_carrying_the_id_still_works(self):
        """The documented shape. Both are handled because the endpoint has
        already changed once, so a revert upstream is not an outage here."""
        respx.get(f"{BASE}/lookup/shows", params={"imdb": "tt2356777"}).mock(
            return_value=httpx.Response(200, json={"id": 5, "name": "True Detective"})
        )

        async with _client() as client:
            assert await client.lookup_show(imdb_id="tt2356777", tvdb_id=None) == 5

    @respx.mock
    async def test_a_404_is_not_a_failure(self):
        """Most of the ~229k mirrored series are not on TV Maze. Raising here
        would count an ordinary answer against the consecutive-failure abort."""
        respx.get(f"{BASE}/lookup/shows").mock(return_value=httpx.Response(404))

        async with _client() as client:
            assert await client.lookup_show(imdb_id="tt0000000", tvdb_id=None) is None

    @respx.mock
    async def test_it_falls_back_from_imdb_to_thetvdb(self):
        respx.get(f"{BASE}/lookup/shows", params={"imdb": "tt0000000"}).mock(
            return_value=httpx.Response(404)
        )
        tvdb = respx.get(f"{BASE}/lookup/shows", params={"thetvdb": "270633"}).mock(
            return_value=httpx.Response(301, headers={"Location": f"{BASE}/shows/5"})
        )

        async with _client() as client:
            assert await client.lookup_show(imdb_id="tt0000000", tvdb_id=270633) == 5
        assert tvdb.called

    @respx.mock
    async def test_a_show_with_no_external_id_makes_no_request(self):
        route = respx.get(f"{BASE}/lookup/shows").mock(return_value=httpx.Response(404))

        async with _client() as client:
            assert await client.lookup_show(imdb_id=None, tvdb_id=None) is None
        assert route.call_count == 0

    @respx.mock
    async def test_a_redirect_somewhere_unexpected_is_no_match_not_a_crash(self, caplog):
        respx.get(f"{BASE}/lookup/shows").mock(
            return_value=httpx.Response(301, headers={"Location": "https://example.com/"})
        )

        with caplog.at_level("WARNING"):
            async with _client() as client:
                assert await client.lookup_show(imdb_id="tt1", tvdb_id=None) is None
        assert "redirected somewhere unexpected" in caplog.text

    @respx.mock
    async def test_a_server_error_still_raises_after_retries(self):
        """A 5xx that survives the retry budget is a persistent upstream
        failure and must still reach the consecutive-failure abort."""
        respx.get(f"{BASE}/lookup/shows").mock(return_value=httpx.Response(500))

        async with _client(retry_max_attempts=2, retry_base_delay=0.0) as client:
            with pytest.raises(httpx.HTTPStatusError):
                await client.lookup_show(imdb_id="tt1", tvdb_id=None)


class TestEpisodes:
    @respx.mock
    async def test_episodes_parse_through_api_payloads(self):
        respx.get(f"{BASE}/shows/5/episodes").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"season": 1, "number": 1, "airdate": "2014-01-12"},
                    # An unscheduled episode: `""` is coerced by OptionalDate at
                    # the parser boundary, not by a guard at the point of use.
                    {"season": 1, "number": 2, "airdate": ""},
                ],
            )
        )

        async with _client() as client:
            episodes = await client.get_show_episodes(5)

        assert [(e.season, e.number, e.airdate) for e in episodes] == [
            (1, 1, date(2014, 1, 12)),
            (1, 2, None),
        ]

    @respx.mock
    async def test_it_does_not_ask_for_specials(self):
        """A null-numbered special cannot be keyed against our side, so every
        one of them would be discarded — fetching them widens the CC BY-SA
        extraction §6 calls deliberately minimised in exchange for nothing."""
        route = respx.get(f"{BASE}/shows/5/episodes").mock(
            return_value=httpx.Response(200, json=[])
        )

        async with _client() as client:
            await client.get_show_episodes(5)

        assert "specials" not in str(route.calls[0].request.url)

    @respx.mock
    async def test_a_missing_show_yields_no_episodes(self):
        respx.get(f"{BASE}/shows/5/episodes").mock(return_value=httpx.Response(404))

        async with _client() as client:
            assert await client.get_show_episodes(5) == []
