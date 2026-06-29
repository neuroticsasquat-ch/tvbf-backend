from sqlalchemy import insert, text

from tvbf.tvmaze import models as m
from tvbf.tvmaze.browse_queries import hydrate_matched_aka, list_shows
from tvbf.tvmaze.schemas import ShowFilters


async def test_unaccent_extension_available(session):
    result = await session.execute(text("SELECT immutable_unaccent('Shōgun')"))
    assert result.scalar_one() == "Shogun"


async def test_search_matches_accented_title_without_accents(session):
    session.add(m.Show(id=70001, name="Shōgun", tvmaze_updated=1))
    await session.commit()
    rows, total = await list_shows(
        session, ShowFilters(search="shogun"), sort="name", page=1, per_page=20
    )
    assert {r.id for r in rows} == {70001}
    assert total == 1


async def test_search_matches_hyphenated_title_as_one_word(session):
    session.add(m.Show(id=70002, name="Spider-Man", tvmaze_updated=1))
    await session.commit()
    rows, _ = await list_shows(
        session, ShowFilters(search="spiderman"), sort="name", page=1, per_page=20
    )
    assert {r.id for r in rows} == {70002}


async def test_search_multitoken_across_punctuation_still_matches(session):
    session.add(m.Show(id=70010, name="Alien: Earth", tvmaze_updated=1))
    session.add(m.Show(id=70011, name="The Office (US)", tvmaze_updated=1))
    await session.commit()
    rows, _ = await list_shows(
        session, ShowFilters(search="alien earth"), sort="name", page=1, per_page=20
    )
    assert 70010 in {r.id for r in rows}
    rows2, _ = await list_shows(
        session, ShowFilters(search="the office us"), sort="name", page=1, per_page=20
    )
    assert 70011 in {r.id for r in rows2}


async def test_search_preserves_non_latin_native_titles(session):
    session.add(m.Show(id=70020, name="進撃の巨人", tvmaze_updated=1))
    await session.commit()
    rows, _ = await list_shows(
        session, ShowFilters(search="進撃"), sort="name", page=1, per_page=20
    )
    assert {r.id for r in rows} == {70020}


async def test_search_punctuation_only_query_returns_nothing(session):
    session.add(m.Show(id=70030, name="Whatever", tvmaze_updated=1))
    await session.commit()
    rows, total = await list_shows(
        session, ShowFilters(search="--"), sort="name", page=1, per_page=20
    )
    assert rows == []
    assert total == 0


async def test_hydrate_matched_aka_folds_accented_aka(session):
    session.add(m.Show(id=70040, name="進撃の巨人", tvmaze_updated=1))
    await session.flush()
    await session.execute(
        insert(m.ShowAka).values(
            show_id=70040,
            name="Attack on Titan",
            country_code="US",
            country_name="United States",
            language="en",
        )
    )
    await session.commit()

    shows, _ = await list_shows(
        session, ShowFilters(search="attack titan"), sort="name", page=1, per_page=20
    )
    matched = await hydrate_matched_aka(session, shows, search="attack titan")
    assert matched == {70040: "Attack on Titan"}


async def test_hydrate_matched_aka_none_when_folded_name_matches(session):
    """A hyphen-folded match carried by the show's own name reports no AKA badge,
    even when the show also has an AKA that would match."""
    session.add(m.Show(id=70041, name="Spider-Man", tvmaze_updated=1))
    await session.flush()
    await session.execute(
        insert(m.ShowAka).values(
            show_id=70041,
            name="Spiderman (US)",
            country_code="US",
            country_name="United States",
            language="en",
        )
    )
    await session.commit()

    shows, _ = await list_shows(
        session, ShowFilters(search="spiderman"), sort="name", page=1, per_page=20
    )
    matched = await hydrate_matched_aka(session, shows, search="spiderman")
    assert matched == {70041: None}
