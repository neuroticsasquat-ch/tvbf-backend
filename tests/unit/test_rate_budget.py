"""The source-neutral parts of the request budget: the in-process limiter and
the `get_rate_limiter` registry. The Postgres bucket itself needs a database
and is covered in `tests/integration/test_rate_budget.py`.
"""

import logging
import time

import pytest

from tvbf.rate_budget import (
    BUCKETS,
    TMDB_BUCKET,
    TVMAZE_BUCKET,
    Bucket,
    Budget,
    DatabaseRateLimiter,
    RateLimiter,
    get_rate_limiter,
    reset_rate_limiters,
)

LOGGER = "tvbf.rate_budget"
TMDB_BUDGET = Budget(20, 1.0, lease=25)
OTHER = "other"
OTHER_BUDGET = Budget(18, 10.0)


@pytest.fixture
def second_source(monkeypatch):
    """Register a second upstream for the duration of one test.

    The registry holds TMDB and — since NEU-1145 re-registered it for the
    airdate oracle — TV Maze, but every per-source property here (a bucket of
    its own, a limiter of its own, no divergence warning across sources) is
    about what happens when a source is *added*. Registering a fixture source
    rather than leaning on whichever two happen to be live is what keeps these
    assertions about the mechanism instead of about today's registry.
    """
    monkeypatch.setitem(
        BUCKETS, OTHER, Bucket(table="catalog.rate_budget", key_column="source", key=OTHER)
    )
    reset_rate_limiters()
    yield OTHER
    reset_rate_limiters()


async def test_rate_limiter_enforces_rate():
    limiter = RateLimiter(calls=3, window_seconds=1)
    start = time.monotonic()
    for _ in range(6):
        await limiter.acquire()
    elapsed = time.monotonic() - start
    assert elapsed >= 1.0, f"6 calls at 3/s should take >= 1s, took {elapsed:.3f}s"


async def test_rate_limiter_acquires_n_slots_at_once():
    """`acquire(n)` spends n slots, however the limiter gets them.

    The in-process limiter has no lease to amortise a block over, so a block of
    3 has to cost the same as three single acquisitions — otherwise swapping it
    in for the database one (which `tests/conftest.py` does) would quietly lift
    the rate for every caller that asks for a block.
    """
    limiter = RateLimiter(calls=3, window_seconds=1)
    start = time.monotonic()
    await limiter.acquire(3)
    await limiter.acquire(3)
    elapsed = time.monotonic() - start
    assert elapsed >= 1.0, f"6 slots at 3/s should take >= 1s, took {elapsed:.3f}s"


def test_every_registered_source_names_a_distinct_bucket():
    """Two sources sharing a row would silently merge their ceilings.

    TMDB fills the mirror; TV Maze is read by the airdate oracle alone
    (NEU-1145) and fills nothing. They are separate ceilings precisely because
    they are separate upstreams, and a shared row would let a nightly oracle
    pass eat a multi-hour ingest's budget.
    """
    assert BUCKETS == {"tmdb": TMDB_BUCKET, "tvmaze": TVMAZE_BUCKET}
    rows = {(b.table, b.key) for b in BUCKETS.values()}
    assert len(rows) == len(BUCKETS)


def test_a_registered_source_still_names_a_distinct_bucket(second_source):
    rows = {(b.table, b.key) for b in BUCKETS.values()}
    assert len(rows) == len(BUCKETS)


def test_an_unregistered_source_raises():
    """Fail closed. A typo that silently got its own unshared bucket is exactly
    the overshoot the registry exists to prevent."""
    with pytest.raises(KeyError):
        get_rate_limiter("tmbd", TMDB_BUDGET)


def test_get_rate_limiter_returns_one_instance_per_budget():
    assert get_rate_limiter("tmdb", TMDB_BUDGET) is get_rate_limiter("tmdb", TMDB_BUDGET)
    assert get_rate_limiter("tmdb", TMDB_BUDGET) is not get_rate_limiter("tmdb", Budget(9, 10.0))


def test_one_budget_written_three_ways_is_one_limiter():
    """`functools.cache` keys on the literal call, not on bound defaults.

    Were the budget three loose arguments, `(20, 1.0)`, `(20, 1.0, 1)` and
    `(20, 1.0, lease=1)` would be three cache entries — so three limiters, each
    holding its own lease against one budget row, which is the overshoot the
    cache exists to prevent. Dataclass equality is what collapses them.
    """
    written_three_ways = (
        Budget(20, 1.0),
        Budget(20, 1.0, 1),
        Budget(20, 1.0, lease=1),
    )
    limiters = {id(get_rate_limiter("tmdb", b)) for b in written_three_ways}
    assert len(limiters) == 1


def test_each_source_gets_its_own_limiter(second_source):
    assert get_rate_limiter(second_source, OTHER_BUDGET) is not get_rate_limiter(
        "tmdb", OTHER_BUDGET
    )


def test_a_second_distinct_budget_for_one_source_warns(caplog):
    """NEU-957: diverging budgets undo the shared limiter, silently.

    Two callers sizing themselves differently get two buckets, which is the
    overshoot NEU-955 removed. Nothing fails, so the warning is the only
    signal anyone gets.
    """
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        get_rate_limiter("tmdb", OTHER_BUDGET)
        get_rate_limiter("tmdb", Budget(9, 10.0))

    warnings = [r for r in caplog.records if r.name == LOGGER]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    # The source and both budgets are named: the one just asked for, and the
    # one already held.
    assert "tmdb" in message
    assert "9 per 10.0s" in message
    assert "Budget(calls=18, window_seconds=10.0, lease=1)" in message


def test_a_lease_that_differs_alone_still_warns(caplog):
    """Same rate, different block size, is still two limiters and two leases."""
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        get_rate_limiter("tmdb", Budget(20, 1.0, lease=25))
        get_rate_limiter("tmdb", Budget(20, 1.0, lease=5))

    assert len([r for r in caplog.records if r.name == LOGGER]) == 1


def test_two_sources_on_different_budgets_stay_quiet(caplog, second_source):
    """Divergence is per source. One upstream's ceiling has nothing to do with
    another's, and warning about the difference would train the warning away."""
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        get_rate_limiter(second_source, OTHER_BUDGET)
        get_rate_limiter("tmdb", TMDB_BUDGET)

    assert [r for r in caplog.records if r.name == LOGGER] == []


def test_one_budget_asked_for_repeatedly_stays_quiet(caplog):
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        first = get_rate_limiter("tmdb", TMDB_BUDGET)
        second = get_rate_limiter("tmdb", TMDB_BUDGET)

    assert first is second
    assert [r for r in caplog.records if r.name == LOGGER] == []


def test_reset_rate_limiters_clears_the_seen_budgets(caplog):
    """Without clearing `_seen_budgets`, the warning would fire on whichever
    test happened to ask for a different budget next."""
    get_rate_limiter("tmdb", OTHER_BUDGET)
    reset_rate_limiters()

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        get_rate_limiter("tmdb", Budget(9, 10.0))

    assert [r for r in caplog.records if r.name == LOGGER] == []
    assert get_rate_limiter.cache_info().currsize == 1


def test_a_lease_smaller_than_one_token_is_rejected():
    """A zero lease would spin: every block would grant nothing and come back."""
    with pytest.raises(ValueError):
        Budget(20, 1.0, lease=0)


async def test_acquiring_fewer_than_one_token_is_rejected():
    limiter = DatabaseRateLimiter(TMDB_BUCKET, Budget(20, 1.0))
    with pytest.raises(ValueError):
        await limiter.acquire(0)
