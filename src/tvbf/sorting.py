"""Shared sorting helpers used by both the `app/` and `tvmaze/` layers."""

import re

_LEADING_ARTICLE_RE = re.compile(r"^(?:the|a|an)\s+", re.IGNORECASE)


def show_name_sort_key(name: str) -> str:
    """Return a sort-friendly form of a show title.

    Lowercases and strips a leading "A ", "An ", or "The " (case-insensitive)
    so that 'The Office' sorts under 'O', 'A Team' under 'T', and 'An Awkward
    Show' under 'A' (the body word, not the article).
    """
    return _LEADING_ARTICLE_RE.sub("", name).lower()


# SQL fragment for the same normalization. Used by tvmaze/browse_queries when
# ordering /shows results so the database matches the Python sort.
SQL_LEADING_ARTICLE_PATTERN = r"^(the|a|an) +"
