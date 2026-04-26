"""Unit tests for the show-name sort key normalizer."""

from tvbf.sorting import show_name_sort_key


def test_strips_leading_the():
    assert show_name_sort_key("The Office") == "office"


def test_strips_leading_a_with_space():
    assert show_name_sort_key("A Team") == "team"


def test_strips_leading_an_with_space():
    assert show_name_sort_key("An Awkward Show") == "awkward show"


def test_case_insensitive_article_match():
    assert show_name_sort_key("the OFFICE") == "office"
    assert show_name_sort_key("THE Wire") == "wire"


def test_does_not_strip_a_when_part_of_word():
    """'Aliens' starts with 'A' but isn't a standalone article."""
    assert show_name_sort_key("Aliens") == "aliens"


def test_does_not_strip_the_when_part_of_word():
    assert show_name_sort_key("Theremin") == "theremin"


def test_only_strips_first_article():
    """A title like 'The A Team' should strip only the leading 'The '."""
    assert show_name_sort_key("The A Team") == "a team"


def test_lowercases_remainder():
    assert show_name_sort_key("Breaking Bad") == "breaking bad"
