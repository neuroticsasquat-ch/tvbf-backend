"""Show search's predicate after NEU-1502: one source per match, short queries
as prefixes, and no wildcard escaping.

The performance half of the ticket is measured with `EXPLAIN (ANALYZE)` on a
full catalog and recorded in the PR — the test database is too small for the
planner to choose those plans, so a plan-shape test here would be a false pin.
What is pinned here is the semantics the rewrite changed or promised.
"""

from sqlalchemy import insert

from tvbf.catalog import models as m
from tvbf.catalog.browse_queries import hydrate_matched_aka, list_shows
from tvbf.catalog.schemas import ShowFilters


async def _search(session, query: str) -> tuple[set[int], list[m.Show], int]:
    rows, total = await list_shows(
        session, ShowFilters(search=query), sort="name", page=1, per_page=100
    )
    return {r.id for r in rows}, rows, total


async def _aka(session, show_id: int, title: str) -> None:
    await session.execute(insert(m.ShowAka).values(show_id=show_id, title=title, country_code="KR"))


# --- §2.1: every token in one source ------------------------------------------


async def test_tokens_split_across_name_and_aka_do_not_match(session):
    """*Phantom Lawyer*'s `office` is in one AKA and `the` in another title —
    before NEU-1502 that was a match the badge could not explain."""
    session.add(m.Show(id=71001, name="Phantom Lawyer"))
    session.add(m.Show(id=71002, name="The Office"))
    await session.flush()
    await _aka(session, 71001, "Shin I-rang Law Office")
    await _aka(session, 71001, "The Phantom Lawyer")
    await session.commit()

    ids, _, total = await _search(session, "the office")
    assert ids == {71002}
    assert total == 1


async def test_a_name_match_and_an_aka_match_are_both_found(session):
    session.add(m.Show(id=71010, name="The Office"))
    session.add(m.Show(id=71011, name="사무실"))
    await session.flush()
    await _aka(session, 71011, "The Office (Korea)")
    await session.commit()

    ids, _, total = await _search(session, "the office")
    assert ids == {71010, 71011}
    assert total == 2


async def test_every_aka_only_result_carries_its_badge(session):
    """The list and `hydrate_matched_aka` build the same predicate, so no result
    comes back without a visible reason."""
    session.add(m.Show(id=71020, name="The Office"))
    session.add(m.Show(id=71021, name="사무실"))
    session.add(m.Show(id=71022, name="Phantom Lawyer"))
    await session.flush()
    await _aka(session, 71021, "The Office (Korea)")
    await _aka(session, 71022, "Shin I-rang Law Office")
    await _aka(session, 71022, "The Phantom Lawyer")
    await session.commit()

    _, rows, _ = await _search(session, "the office")
    badges = await hydrate_matched_aka(session, rows, search="the office")
    assert badges == {71020: None, 71021: "The Office (Korea)"}


# --- §2.2: short queries match the start of a title ---------------------------


async def test_short_query_matches_a_title_prefix_not_a_substring(session):
    session.add(m.Show(id=71030, name="ER"))
    session.add(m.Show(id=71031, name="Cheers"))
    await session.commit()

    ids, _, _ = await _search(session, "er")
    assert ids == {71030}


async def test_short_query_runs_its_tokens_together(session):
    session.add(m.Show(id=71040, name="24 H"))
    session.add(m.Show(id=71041, name="Room 24"))
    await session.commit()

    ids, _, _ = await _search(session, "24 h")
    assert ids == {71040}


async def test_one_character_query(session):
    session.add(m.Show(id=71050, name="V"))
    session.add(m.Show(id=71051, name="Dave"))
    await session.commit()

    ids, _, _ = await _search(session, "v")
    assert ids == {71050}


async def test_short_query_matches_an_aka_prefix_and_badges_it(session):
    session.add(m.Show(id=71060, name="Emergency Room"))
    await session.flush()
    await _aka(session, 71060, "ER")
    await session.commit()

    ids, rows, _ = await _search(session, "er")
    assert ids == {71060}
    assert await hydrate_matched_aka(session, rows, search="er") == {71060: "ER"}


async def test_one_long_token_makes_it_a_substring_search(session):
    """`office` is long enough, so `us` rides along as an ordinary substring."""
    session.add(m.Show(id=71070, name="The Office (US)"))
    session.add(m.Show(id=71071, name="The Office (UK)"))
    await session.commit()

    ids, _, _ = await _search(session, "office us")
    assert ids == {71070}


# --- §2.3: wildcards are punctuation, and the fold strips them ------------------


async def test_wildcards_are_folded_away_not_interpreted(session):
    """`a%b` searches for the literal `ab` — the `%` is gone before the pattern
    is built — so it cannot act as a wildcard across the gap in *A x B*."""
    session.add(m.Show(id=71080, name="ABC Mysteries"))
    session.add(m.Show(id=71081, name="A x B"))
    await session.commit()

    ids, _, _ = await _search(session, "a%b")
    assert ids == {71080}


async def test_a_bare_wildcard_matches_nothing(session):
    session.add(m.Show(id=71090, name="Anything"))
    await session.commit()

    ids, _, total = await _search(session, "%")
    assert ids == set()
    assert total == 0


async def test_a_combining_accent_does_not_lengthen_a_short_query(session):
    """`e` + U+0301 folds to `e`, so `ér` typed decomposed is still the short
    query `er` — a prefix, which finds *ER* and not *Cheers*."""
    session.add(m.Show(id=71100, name="ER"))
    session.add(m.Show(id=71101, name="Cheers"))
    await session.commit()

    ids, _, _ = await _search(session, "ér")
    assert ids == {71100}
