"""The three-tier `tmdb_id` mapping (NEU-1043).

Every test here is one of the ticket's acceptance criteria or one of the ways
the mapping could silently attach a user's watch history to the wrong show. The
real `TMDBClient` is exercised through `respx` rather than a stub, so the
request shape each tier sends is part of what is asserted.
"""

from datetime import date

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from tvbf.catalog import models as m
from tvbf.tmdb.client import TMDBClient
from tvbf.tmdb.enrichment import (
    MATCH_IMDB_ID,
    MATCH_TITLE_YEAR,
    MATCH_TVDB_ID,
    enrich_show_ids,
)

BASE = "https://api.themoviedb.org/3"


def _client() -> TMDBClient:
    return TMDBClient(
        base_url=BASE,
        read_access_token="eyJ-not-a-real-token",
        rate_calls=20,
        rate_window=1,
    )


async def _show(session, **kwargs) -> m.Show:
    show = m.Show(name=kwargs.pop("name", "A Show"), **kwargs)
    session.add(show)
    await session.commit()
    return show


async def _run(session, *, limit: int | None = None):
    async with _client() as client:
        return await enrich_show_ids(session, client, limit=limit, batch_size=2)


async def _reload(session, show_id: int) -> m.Show:
    stmt = select(m.Show).where(m.Show.id == show_id).execution_options(populate_existing=True)
    return (await session.execute(stmt)).scalar_one()


def _find(external_source: str, external_id: str, series: list[dict]) -> respx.Route:
    return respx.get(
        f"{BASE}/find/{external_id}", params={"external_source": external_source}
    ).mock(return_value=httpx.Response(200, json={"tv_results": series}))


def _search(results: list[dict], total: int | None = None) -> respx.Route:
    return respx.get(f"{BASE}/search/tv").mock(
        return_value=httpx.Response(
            200,
            json={"results": results, "total_results": len(results) if total is None else total},
        )
    )


# --- tier 1: /find by tvdb_id ----------------------------------------------


@respx.mock
async def test_tvdb_id_match_records_its_tier(session):
    _find("tvdb_id", "355567", [{"id": 1396}])
    await _show(session, id=1, name="Breaking Bad", tvdb_id=355567)

    result = await _run(session)

    show = await _reload(session, 1)
    assert (show.tmdb_id, show.match_method) == (1396, MATCH_TVDB_ID)
    assert result.by_method[MATCH_TVDB_ID] == 1
    assert result.matched == 1


@respx.mock
async def test_ambiguous_find_result_is_not_matched(session):
    """Two TMDB series claiming one tvdb_id is a conflict, not a coin flip."""
    _find("tvdb_id", "999", [{"id": 1}, {"id": 2}])
    await _show(session, id=1, tvdb_id=999)

    result = await _run(session)

    show = await _reload(session, 1)
    assert (show.tmdb_id, show.match_method) == (None, None)
    assert result.unmatched == 1


@respx.mock
async def test_an_ambiguous_exact_lookup_never_falls_through_to_a_guess(session):
    """The contested case must not be answered by the weakest tier.

    Upstream returning two series for our `tvdb_id` is the strongest available
    signal that it does not know which one this row is. Falling through would
    let a title search settle it — resolving the clearest ambiguity with a
    guess, which is the thing the mapping rule forbids.
    """
    _find("tvdb_id", "999", [{"id": 1}, {"id": 2}])
    search = _search([{"id": 500, "name": "A Show", "first_air_date": "2005-01-01"}])
    await _show(session, id=1, name="A Show", tvdb_id=999, first_air_date=date(2005, 1, 1))

    result = await _run(session)

    show = await _reload(session, 1)
    assert (show.tmdb_id, show.match_method) == (None, None)
    assert search.call_count == 0
    assert result.unmatched == 1


@respx.mock
async def test_an_ambiguous_tvdb_lookup_does_not_fall_through_to_imdb_either(session):
    """Same reasoning one tier up: tier 2 is not a second opinion on a conflict."""
    _find("tvdb_id", "999", [{"id": 1}, {"id": 2}])
    imdb = _find("imdb_id", "tt0903747", [{"id": 3}])
    await _show(session, id=1, tvdb_id=999, imdb_id="tt0903747")

    result = await _run(session)

    assert (await _reload(session, 1)).tmdb_id is None
    assert imdb.call_count == 0
    assert result.unmatched == 1


# --- tier 2: /find by imdb_id ----------------------------------------------


@respx.mock
async def test_imdb_id_picks_up_a_show_tvdb_missed(session):
    _find("tvdb_id", "355567", [])
    _find("imdb_id", "tt0903747", [{"id": 1396}])
    await _show(session, id=1, tvdb_id=355567, imdb_id="tt0903747")

    result = await _run(session)

    show = await _reload(session, 1)
    assert (show.tmdb_id, show.match_method) == (1396, MATCH_IMDB_ID)
    assert result.by_method == {MATCH_TVDB_ID: 0, MATCH_IMDB_ID: 1, MATCH_TITLE_YEAR: 0}


# --- tier 3: /search/tv by title + year ------------------------------------


@respx.mock
async def test_single_exact_title_within_a_year_is_matched(session):
    _search([{"id": 1396, "name": "Breaking Bad", "first_air_date": "2008-01-20"}])
    await _show(session, id=1, name="Breaking Bad", first_air_date=date(2009, 1, 20))

    result = await _run(session)

    show = await _reload(session, 1)
    assert (show.tmdb_id, show.match_method) == (1396, MATCH_TITLE_YEAR)
    assert result.by_method[MATCH_TITLE_YEAR] == 1


@respx.mock
async def test_two_plausible_results_leave_the_show_unmatched(session):
    """The ticket's criterion, stated as itself: a second candidate means stop."""
    _search(
        [
            {"id": 1, "name": "The Office", "first_air_date": "2005-03-24"},
            {"id": 2, "name": "The Office", "first_air_date": "2005-07-09"},
        ]
    )
    await _show(session, id=1, name="The Office", first_air_date=date(2005, 3, 24))

    result = await _run(session)

    assert (await _reload(session, 1)).tmdb_id is None
    assert result.unmatched == 1


@respx.mock
async def test_a_paged_total_beyond_one_is_rejected_even_with_one_result_shown(session):
    """`total_results` is the condition; the page length is not a substitute."""
    _search([{"id": 1, "name": "A Show", "first_air_date": "2005-01-01"}], total=37)
    await _show(session, id=1, name="A Show", first_air_date=date(2005, 1, 1))

    assert (await _run(session)).unmatched == 1
    assert (await _reload(session, 1)).tmdb_id is None


@respx.mock
async def test_a_year_two_off_is_rejected(session):
    _search([{"id": 1, "name": "A Show", "first_air_date": "2007-01-01"}])
    await _show(session, id=1, name="A Show", first_air_date=date(2005, 1, 1))

    assert (await _run(session)).unmatched == 1
    assert (await _reload(session, 1)).tmdb_id is None


@respx.mock
async def test_a_missing_upstream_air_date_is_rejected(session):
    _search([{"id": 1, "name": "A Show", "first_air_date": ""}])
    await _show(session, id=1, name="A Show", first_air_date=date(2005, 1, 1))

    assert (await _run(session)).unmatched == 1


@respx.mock
async def test_a_different_title_in_the_same_year_is_rejected(session):
    _search([{"id": 1, "name": "A Show Reborn", "first_air_date": "2005-01-01"}])
    await _show(session, id=1, name="A Show", first_air_date=date(2005, 1, 1))

    assert (await _run(session)).unmatched == 1


@respx.mock
async def test_a_title_differing_only_by_the_fold_is_matched(session):
    """The comparison folds through Postgres — `ō` and `-` cannot break a match."""
    _search([{"id": 55, "name": "Shōgun", "first_air_date": "2024-02-27"}])
    await _show(session, id=1, name="Shogun", first_air_date=date(2024, 2, 27))

    await _run(session)

    show = await _reload(session, 1)
    assert (show.tmdb_id, show.match_method) == (55, MATCH_TITLE_YEAR)


@respx.mock
async def test_a_show_with_no_premiere_date_never_reaches_the_search(session):
    """Excluded from tier 3 entirely — not merely rejected after the request."""
    search = _search([{"id": 1, "name": "A Show", "first_air_date": "2005-01-01"}])
    await _show(session, id=1, name="A Show", first_air_date=None)

    result = await _run(session)

    assert search.call_count == 0
    assert result.unmatched == 1


# --- writing, idempotence and collisions ------------------------------------


@respx.mock
async def test_a_rerun_does_not_touch_an_existing_exact_match(session):
    find = _find("tvdb_id", "355567", [{"id": 1396}])
    await _show(session, id=1, name="Breaking Bad", tvdb_id=355567)
    await _run(session)

    second = await _run(session)

    show = await _reload(session, 1)
    assert (show.tmdb_id, show.match_method) == (1396, MATCH_TVDB_ID)
    # Not merely unchanged — not even reconsidered, so a re-run costs nothing.
    assert find.call_count == 1
    assert second.considered == 0


@respx.mock
async def test_a_second_show_matching_the_same_series_is_left_unmatched(session):
    """TV Maze carries duplicate show entries; one must not overwrite the other."""
    _find("tvdb_id", "1", [{"id": 1396}])
    _find("tvdb_id", "2", [{"id": 1396}])
    await _show(session, id=1, name="Breaking Bad", tvdb_id=1)
    await _show(session, id=2, name="Breaking Bad (dup)", tvdb_id=2)

    result = await _run(session)

    assert (await _reload(session, 1)).tmdb_id == 1396
    duplicate = await _reload(session, 2)
    assert (duplicate.tmdb_id, duplicate.match_method) == (None, None)
    assert result.collisions == 1
    assert result.matched == 1


@respx.mock
async def test_limit_caps_how_many_shows_are_considered(session):
    _find("tvdb_id", "1", [{"id": 11}])
    _find("tvdb_id", "2", [{"id": 22}])
    await _show(session, id=1, tvdb_id=1)
    await _show(session, id=2, tvdb_id=2)

    result = await _run(session, limit=1)

    assert result.considered == 1
    assert (await _reload(session, 2)).tmdb_id is None


@respx.mock
async def test_an_unmatched_show_is_reconsidered_on_the_next_run(session):
    """The residue is retried for free — that is how a newly added series lands."""
    route = _find("tvdb_id", "1", [])
    await _show(session, id=1, tvdb_id=1)
    await _run(session)

    _find("tvdb_id", "1", [{"id": 77}])
    await _run(session)

    assert (await _reload(session, 1)).tmdb_id == 77
    # Re-registering the same pattern returns the same respx route, so its two
    # calls are the two runs: the failure genuinely cost another request.
    assert route.call_count == 2


@pytest.mark.parametrize("method", [MATCH_TVDB_ID, MATCH_IMDB_ID, MATCH_TITLE_YEAR])
async def test_every_tier_name_satisfies_the_check_constraint(session, method):
    """The vocabulary in the code and the one in the database are the same three."""
    await _show(session, id=1, tmdb_id=1, match_method=method)
    assert (await _reload(session, 1)).match_method == method


async def test_an_invented_match_method_is_rejected(session):
    """The vocabulary is ours, so a typo is a bug the database should catch."""
    with pytest.raises(IntegrityError):
        await _show(session, id=1, tmdb_id=1, match_method="vibes")
