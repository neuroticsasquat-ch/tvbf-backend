import pytest
from sqlalchemy import insert

from tvbf.tvmaze import models as m
from tvbf.tvmaze.browse_queries import hydrate_matched_aka, list_shows
from tvbf.tvmaze.dto import ShowFilters


@pytest.fixture
async def seeded_shows(session):
    """One Japanese show with English AKAs, one English-titled, one foreign with no AKAs."""
    rows = [
        m.Show(id=1, name="東京リベンジャーズ", tvmaze_updated=1),
        m.Show(id=2, name="Severance", tvmaze_updated=1),
        m.Show(id=3, name="進撃の巨人", tvmaze_updated=1),
    ]
    for r in rows:
        session.add(r)
    await session.flush()  # required: see CLAUDE.md "no relationship() — explicit flush"
    await session.execute(
        insert(m.ShowAka).values(
            [
                {
                    "show_id": 1,
                    "name": "Tokyo Revengers",
                    "country_code": "US",
                    "country_name": "United States",
                    "language": "en",
                },
                {
                    "show_id": 1,
                    "name": "Tokyo Revengers",
                    "country_code": "GB",
                    "country_name": "United Kingdom",
                    "language": "en",
                },
            ]
        )
    )
    await session.commit()


async def test_search_matches_show_name(session, seeded_shows):
    shows, total = await list_shows(
        session, ShowFilters(search="severance"), sort="name", page=1, per_page=20
    )
    assert total == 1
    assert {s.id for s in shows} == {2}


async def test_search_matches_aka_name(session, seeded_shows):
    shows, total = await list_shows(
        session,
        ShowFilters(search="tokyo revengers"),
        sort="name",
        page=1,
        per_page=20,
    )
    assert total == 1
    assert {s.id for s in shows} == {1}


async def test_search_dedupes_when_show_has_multiple_aka_matches(session, seeded_shows):
    shows, total = await list_shows(
        session, ShowFilters(search="tokyo"), sort="name", page=1, per_page=20
    )
    assert total == 1  # not 2 — even though show 1 has two matching AKA rows
    assert {s.id for s in shows} == {1}


async def test_search_returns_only_shows_matching_native_when_no_aka(session, seeded_shows):
    shows, total = await list_shows(
        session, ShowFilters(search="進撃"), sort="name", page=1, per_page=20
    )
    assert total == 1
    assert {s.id for s in shows} == {3}


async def test_search_returns_empty_for_unrelated_terms(session, seeded_shows):
    shows, total = await list_shows(
        session, ShowFilters(search="frieren"), sort="name", page=1, per_page=20
    )
    assert total == 0
    assert shows == []


# ---------------------------------------------------------------------------
# hydrate_matched_aka — surfaces which AKA matched when the show.name didn't.
# ---------------------------------------------------------------------------


async def test_hydrate_matched_aka_populates_when_only_aka_matches(session, seeded_shows):
    shows, _ = await list_shows(
        session, ShowFilters(search="tokyo revengers"), sort="name", page=1, per_page=20
    )
    matched = await hydrate_matched_aka(session, shows, search="tokyo revengers")
    assert matched == {1: "Tokyo Revengers"}


async def test_hydrate_matched_aka_is_none_when_show_name_matches(session, seeded_shows):
    shows, _ = await list_shows(
        session, ShowFilters(search="severance"), sort="name", page=1, per_page=20
    )
    matched = await hydrate_matched_aka(session, shows, search="severance")
    assert matched == {2: None}


async def test_hydrate_matched_aka_returns_empty_dict_when_no_search(session, seeded_shows):
    shows, _ = await list_shows(session, ShowFilters(), sort="name", page=1, per_page=20)
    matched = await hydrate_matched_aka(session, shows, search=None)
    assert matched == {}


async def test_hydrate_matched_aka_picks_one_per_show_when_multiple_akas_match(session):
    """When a show has multiple matching AKAs, pick exactly one (deterministic)."""
    session.add(m.Show(id=42, name="進撃の巨人", tvmaze_updated=1))
    await session.flush()
    await session.execute(
        insert(m.ShowAka).values(
            [
                {"show_id": 42, "name": "Attack on Titan", "country_code": "US"},
                {"show_id": 42, "name": "Attack on Titan: Final Season", "country_code": "GB"},
                {"show_id": 42, "name": "AoT", "country_code": "JP"},
            ]
        )
    )
    await session.commit()

    shows, _ = await list_shows(
        session, ShowFilters(search="attack on titan"), sort="name", page=1, per_page=20
    )
    matched = await hydrate_matched_aka(session, shows, search="attack on titan")
    # Picks the shortest matching AKA — most likely the canonical title.
    assert matched == {42: "Attack on Titan"}


async def test_hydrate_matched_aka_handles_mixed_page(session, seeded_shows):
    """A page with both name-matchers and AKA-matchers carries matched_aka only on AKA-matchers."""
    # Add another show whose name matches the same token used to find an AKA-matcher.
    session.add(m.Show(id=99, name="Tokyo Vice", tvmaze_updated=1))
    await session.commit()

    shows, _ = await list_shows(
        session, ShowFilters(search="tokyo"), sort="name", page=1, per_page=20
    )
    matched = await hydrate_matched_aka(session, shows, search="tokyo")
    # Show 1 (東京リベンジャーズ) matched on AKA "Tokyo Revengers".
    # Show 99 (Tokyo Vice) matched on its own name → None.
    assert matched[1] == "Tokyo Revengers"
    assert matched[99] is None
