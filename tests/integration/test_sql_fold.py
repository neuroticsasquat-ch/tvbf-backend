"""`folded_equal` — the one predicate that decides "same title" for NEU-1043.

It runs in Postgres because it cannot run anywhere else: the ł/ø cases below are
exactly the ones a Python-side `unaccent` gets wrong, and getting them wrong
would attach a user's watch history to the wrong show.
"""

import pytest

from tvbf.sql_fold import folded_equal


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Shōgun", "Shogun"),
        ("Spider-Man", "Spiderman"),
        ("The Office (US)", "the office us"),
        ("Æon Flux", "Aeon Flux"),
        # NFKD does not decompose these, which is the whole reason the fold is
        # not reimplementable in Python.
        ("Łódź", "Lodz"),
        ("Smørrebrød", "Smorrebrod"),
    ],
)
async def test_titles_differing_only_by_fold_are_equal(session, left, right):
    assert await folded_equal(session, left, right) is True


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("The Office", "The Office UK"),
        ("Shogun", "Shoguns"),
        ("", "anything"),
    ],
)
async def test_genuinely_different_titles_are_not_equal(session, left, right):
    assert await folded_equal(session, left, right) is False


async def test_titles_that_fold_to_nothing_never_match(session):
    """Two unrelated punctuation-only titles must not read as the same show."""
    assert await folded_equal(session, "!!!", "???") is False
    assert await folded_equal(session, "", "") is False


async def test_non_latin_scripts_pass_through_unchanged(session):
    """Native-title matching has to keep working — the fold must not eat it."""
    assert await folded_equal(session, "След", "След") is True
    assert await folded_equal(session, "След", "Слово") is False
