import pytest
from sqlalchemy import insert

from tvbf.tvmaze import models as m
from tvbf.tvmaze.browse_queries import list_shows
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
