"""Query-level tests for person search (NEU-950).

Person search reuses the folded-match machinery show search already uses, so
these mirror `test_search_normalization.py` — pointed at `tvmaze.person`.
"""

from tvbf.tvmaze import models as m
from tvbf.tvmaze.browse_queries import list_shows, search_people
from tvbf.tvmaze.schemas import ShowFilters


async def _add_people(session, *names: tuple[int, str]) -> None:
    session.add_all([m.Person(id=pid, name=name, tvmaze_updated=1) for pid, name in names])
    await session.commit()


async def test_search_matches_accented_name_without_accents(session):
    # The whole reason folding matters more for names than titles: nobody types
    # the diacritics, and the catalog is full of them.
    await _add_people(session, (80001, "Goran Višnjić"), (80002, "Someone Else"))
    rows, total = await search_people(session, "visnjic", page=1, per_page=20)
    assert [r.id for r in rows] == [80001]
    assert total == 1


async def test_search_matches_across_punctuation(session):
    await _add_people(session, (80010, "Jean-Luc Picard"))
    rows, _ = await search_people(session, "jeanluc", page=1, per_page=20)
    assert [r.id for r in rows] == [80010]


async def test_search_tokens_are_anded(session):
    # The ticket's own case: "zachary levi" matches, "zachary garcia" does not.
    await _add_people(session, (80020, "Zachary Levi"))
    rows, _ = await search_people(session, "zachary levi", page=1, per_page=20)
    assert [r.id for r in rows] == [80020]

    rows, total = await search_people(session, "zachary garcia", page=1, per_page=20)
    assert rows == [] and total == 0


async def test_every_token_must_match_the_same_person(session):
    # Two people who between them carry both tokens must not satisfy one query.
    await _add_people(session, (80025, "Zachary Levi"), (80026, "Adam Garcia"))
    rows, total = await search_people(session, "zachary garcia", page=1, per_page=20)
    assert rows == [] and total == 0


async def test_search_tokens_match_in_any_order(session):
    await _add_people(session, (80030, "Zachary Levi"))
    rows, _ = await search_people(session, "levi zachary", page=1, per_page=20)
    assert [r.id for r in rows] == [80030]


async def test_punctuation_only_query_returns_nothing(session):
    # The guard that matters: a query folding to nothing must match nothing, not
    # hand back all 487k people.
    await _add_people(session, (80040, "Somebody"), (80041, "Somebody Else"))
    rows, total = await search_people(session, "--", page=1, per_page=20)
    assert rows == [] and total == 0


async def test_search_preserves_non_latin_names(session):
    await _add_people(session, (80050, "宮崎駿"))
    rows, _ = await search_people(session, "宮崎", page=1, per_page=20)
    assert [r.id for r in rows] == [80050]


async def test_absent_or_blank_search_matches_nothing(session):
    """Search-only by design: no usable token means an empty page, never the
    whole 487k-row table."""
    await _add_people(session, (80060, "Alpha"), (80061, "Beta"))
    for term in (None, "", "   "):
        rows, total = await search_people(session, term, page=1, per_page=20)
        assert rows == [] and total == 0


async def test_results_are_alphabetical_and_case_insensitive(session):
    await _add_people(session, (80070, "zoe Xu"), (80071, "Alan Xu"), (80072, "Molly Xu"))
    rows, _ = await search_people(session, "xu", page=1, per_page=20)
    assert [r.name for r in rows] == ["Alan Xu", "Molly Xu", "zoe Xu"]


async def test_pagination_slices_a_stable_total(session):
    await _add_people(
        session,
        (80080, "Match One"),
        (80081, "Match Two"),
        (80082, "Match Three"),
        (80083, "Unrelated"),
    )
    page1, total1 = await search_people(session, "match", page=1, per_page=2)
    page2, total2 = await search_people(session, "match", page=2, per_page=2)
    # Total is the full match count, not the page size.
    assert total1 == total2 == 3
    assert [p.name for p in page1] == ["Match One", "Match Three"]
    assert [p.name for p in page2] == ["Match Two"]


async def test_page_past_the_end_is_empty_not_an_error(session):
    await _add_people(session, (80090, "Only One"))
    rows, total = await search_people(session, "only", page=5, per_page=20)
    assert rows == [] and total == 1


async def test_show_search_is_unaffected_by_person_names(session):
    """Regression guard for the design decision in NEU-947: person names are a
    separate entity search, never a third OR branch in show search. If someone
    later folds `person.name` into `list_shows`, this fails."""
    session.add(m.Show(id=80100, name="Chuck", tvmaze_updated=1))
    session.add(m.Show(id=80101, name="Zachary's Diary", tvmaze_updated=1))
    await session.commit()
    await _add_people(session, (80102, "Zachary Levi"))

    # A person's full name must not pull in the show they star in...
    rows, total = await list_shows(
        session, ShowFilters(search="zachary levi"), sort="name", page=1, per_page=20
    )
    assert rows == [] and total == 0

    # ...and title search still behaves exactly as it did.
    rows, _ = await list_shows(
        session, ShowFilters(search="zachary"), sort="name", page=1, per_page=20
    )
    assert [r.id for r in rows] == [80101]
    rows, _ = await list_shows(
        session, ShowFilters(search="chuck"), sort="name", page=1, per_page=20
    )
    assert [r.id for r in rows] == [80100]
