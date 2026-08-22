"""How the catalog ingest spends its `append_to_response` budget (NEU-1034).

The arithmetic that decides whether a show costs one request or two, kept
separate from the ingest's DB-backed behaviour because it is neither.
"""

from tvbf.catalog import models as m
from tvbf.tmdb.client import APPEND_TO_RESPONSE_LIMIT, DEFAULT_APPEND, plan_append
from tvbf.tmdb.ingest import SPECULATIVE_SEASONS


def test_the_speculative_window_is_exactly_what_the_namespaces_leave():
    """Eight slots after the twelve namespaces, so seasons 0..7.

    Derived rather than written out, and NEU-1052 is the case that proves it: it
    added the twelfth namespace (`recommendations`) and the window narrowed on
    its own instead of pushing the request past the 20-entry cap into a hard 400.

    The cost of that narrowing was measured rather than assumed — across all
    210,343 mirrored shows carrying ingested seasons, 96.84% fit inside 0..8 and
    96.34% inside 0..7, so 1,054 shows pay one extra `get_tv_season` each.
    """
    assert SPECULATIVE_SEASONS == (0, 1, 2, 3, 4, 5, 6, 7)
    assert len(SPECULATIVE_SEASONS) == APPEND_TO_RESPONSE_LIMIT - len(DEFAULT_APPEND)


def test_the_first_request_fits_the_cap_with_nothing_left_over():
    append, overflow = plan_append(SPECULATIVE_SEASONS)

    assert len(append) == APPEND_TO_RESPONSE_LIMIT
    assert not overflow, "the window is sized to fit, so nothing should spill here"
    assert append[len(DEFAULT_APPEND) :] == [
        f"season/{n}" for n in range(0, APPEND_TO_RESPONSE_LIMIT - len(DEFAULT_APPEND))
    ]


def test_the_window_starts_at_specials():
    """Measured 2026-08-10 (`scripts/probe_tmdb_season_speculation.py`): across
    200 sampled series, 0..8 covers 97.5% against 94.0% for 1..9. Shows with a
    ninth numbered season are rarer than shows with specials."""
    assert SPECULATIVE_SEASONS[0] == 0


def test_the_resumability_watermark_has_a_column():
    """The model and the migration have to stay in step: the suite builds from
    `create_all` and never sees the migration, so a column added to only one of
    them would pass every other test in the suite."""
    assert "tmdb_synced_at" in m.Show.__table__.c
