"""NEU-1005 — shows deleted upstream are tombstoned, never deleted (ADR-0005)."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from tests.fixtures.spines import mirror_spine
from tvbf.app import models as am
from tvbf.tvmaze import models as m
from tvbf.tvmaze.tombstone import (
    _MIN_FEED_ABSOLUTE,
    TombstoneResult,
    reconcile_tombstones,
)

# The floors only permit a realistic feed, so tests need a realistic one. Build
# a filler set of ids well clear of the ids under test.
_FILLER = set(range(500_000, 500_000 + _MIN_FEED_ABSOLUTE))


def _feed(*show_ids: int) -> set[int]:
    """A plausible feed that contains exactly the given shows, plus filler."""
    return _FILLER | set(show_ids)


async def _add_show(session, show_id: int, *, deleted: bool = False) -> None:
    session.add(
        m.Show(
            id=show_id,
            name=f"Show {show_id}",
            tvmaze_updated=1700000000,
            deleted_upstream_at=datetime.now(UTC) if deleted else None,
        )
    )
    await session.flush()


async def _deleted_at(session, show_id: int) -> datetime | None:
    return (
        await session.execute(
            select(m.Show.deleted_upstream_at)
            .where(m.Show.id == show_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def test_show_absent_from_the_feed_is_tombstoned(session):
    await _add_show(session, 700)  # present upstream
    await _add_show(session, 701)  # gone upstream
    await session.commit()

    result = await reconcile_tombstones(session, feed_ids=_feed(700))
    await session.commit()

    assert result == TombstoneResult(tombstoned=1, resurrected=0)
    assert await _deleted_at(session, 700) is None
    assert await _deleted_at(session, 701) is not None


async def test_a_show_that_reappears_upstream_is_resurrected(session):
    await _add_show(session, 710, deleted=True)
    await session.commit()

    result = await reconcile_tombstones(session, feed_ids=_feed(710))
    await session.commit()

    assert result == TombstoneResult(tombstoned=0, resurrected=1)
    assert await _deleted_at(session, 710) is None


async def test_already_tombstoned_show_is_not_re_stamped(session):
    """The counter reports work done, so a steady state must report zero."""
    await _add_show(session, 715, deleted=True)
    await session.commit()
    before = await _deleted_at(session, 715)

    result = await reconcile_tombstones(session, feed_ids=_feed())
    await session.commit()

    assert result == TombstoneResult(tombstoned=0, resurrected=0)
    assert await _deleted_at(session, 715) == before


@pytest.mark.parametrize(
    "feed,label",
    [
        (set(), "empty"),
        (set(range(1, 10)), "tiny"),
    ],
)
async def test_feed_under_the_absolute_floor_writes_nothing(session, feed, label, caplog):
    """Trap: a truncated 200 would otherwise tombstone the whole catalogue."""
    caplog.set_level("ERROR", logger="tvbf.tvmaze.tombstone")
    await _add_show(session, 720)
    await session.commit()

    result = await reconcile_tombstones(session, feed_ids=feed)
    await session.commit()

    assert result.tombstoned == 0
    assert result.resurrected == 0
    assert result.skipped_reason is not None
    assert "absolute floor" in result.skipped_reason
    assert await _deleted_at(session, 720) is None, f"{label} feed must not tombstone"
    # The skip must be loud — it is the only signal that the guard fired.
    assert any(r.levelname == "ERROR" and "wrote nothing" in r.message for r in caplog.records), (
        "a skipped tombstone pass must log an error"
    )


async def test_an_untrusted_feed_does_not_resurrect_either(session):
    """A feed we won't trust to prove absence can't be trusted to prove presence."""
    await _add_show(session, 740, deleted=True)
    await session.commit()

    result = await reconcile_tombstones(session, feed_ids={740})
    await session.commit()

    assert result.resurrected == 0
    assert result.skipped_reason is not None
    assert await _deleted_at(session, 740) is not None


async def test_tombstoning_never_deletes_a_row(session):
    """The test that fails if anyone reintroduces a DELETE."""
    await _add_show(session, 750)
    await session.flush()
    session.add(m.Season(id=7500, show_id=750, number=1))
    await session.flush()
    session.add(m.Episode(id=75000, show_id=750, season_id=7500, season=1, number=1))
    await session.commit()

    before = [
        (await session.execute(select(func.count()).select_from(model))).scalar_one()
        for model in (m.Show, m.Season, m.Episode)
    ]

    await reconcile_tombstones(session, feed_ids=_feed())
    await session.commit()

    after = [
        (await session.execute(select(func.count()).select_from(model))).scalar_one()
        for model in (m.Show, m.Season, m.Episode)
    ]
    assert before == after, "tombstoning must not remove show, season or episode rows"
    assert await _deleted_at(session, 750) is not None


async def test_a_tracked_shows_user_data_survives_tombstoning(session, make_user):
    """The acceptance criterion: tombstoning must never cost a user their data.

    Would fail if anyone swapped the tombstone for a DELETE. Since NEU-1046 the
    cascades run from `catalog` rather than `tvmaze`, so a DELETE here would no
    longer take the watch rows with it directly — but the id-preserving copy
    means the same show is a `catalog.show` row, and NEU-1050 drops this schema
    on the assumption nothing here is load-bearing. The mirror below is what
    makes the assertion about the constraint that now exists.
    """
    user = await make_user(email="tombstone@example.com")
    await _add_show(session, 760)
    await session.flush()
    session.add(m.Season(id=7600, show_id=760, number=1))
    await session.flush()
    session.add(m.Episode(id=76000, show_id=760, season_id=7600, season=1, number=1))
    await session.flush()
    await mirror_spine(session)
    session.add_all(
        [
            am.UserShowWatch(user_id=user.id, show_id=760),
            am.UserEpisodeWatch(user_id=user.id, episode_id=76000),
            am.UserShowRating(user_id=user.id, show_id=760, stars=4.5),
        ]
    )
    await session.commit()

    await reconcile_tombstones(session, feed_ids=_feed())
    await session.commit()

    assert await _deleted_at(session, 760) is not None, "show should be tombstoned"

    assert (
        await session.execute(
            select(func.count())
            .select_from(am.UserShowWatch)
            .where(am.UserShowWatch.user_id == user.id)
        )
    ).scalar_one() == 1
    assert (
        await session.execute(
            select(func.count())
            .select_from(am.UserEpisodeWatch)
            .where(am.UserEpisodeWatch.user_id == user.id)
        )
    ).scalar_one() == 1
    assert (
        await session.execute(
            select(func.count())
            .select_from(am.UserShowRating)
            .where(am.UserShowRating.user_id == user.id)
        )
    ).scalar_one() == 1
