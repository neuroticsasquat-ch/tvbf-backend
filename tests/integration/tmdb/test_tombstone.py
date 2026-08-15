"""NEU-1036 — series absent from the id export are tombstoned, never deleted.

ADR-0005 against the TMDB spine. What has to hold: a truncated export writes
nothing and says so loudly, a series that comes back is resurrected, and a
locally-authored row is never touched — that last one being the mechanism
protecting watch history TMDB cannot supply.
"""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from sqlalchemy import func, select

from tests.integration.tmdb.test_ingest import BASE, _run_row, mock_series
from tests.integration.tmdb.test_update import TODAY, _run, _seed_cursor, mock_changes
from tvbf.catalog import models as m
from tvbf.tmdb.tombstone import (
    _MEASURED_EXPORT,
    _MIN_FEED_ABSOLUTE,
    TombstoneResult,
    reconcile_tombstones,
)

# The floors only permit a realistic export, so tests need a realistic one —
# full size, since the relative floor measures against the catalog TMDB is known
# to hold rather than against the handful of rows a test seeds. Filler ids sit
# well clear of the ones under test.
_FILLER = set(range(500_000, 500_000 + _MEASURED_EXPORT))


def _export(*tmdb_ids: int) -> set[int]:
    """A plausible export containing exactly the given series, plus filler."""
    return _FILLER | set(tmdb_ids)


async def _add_show(session, *, tmdb_id: int | None, deleted: bool = False) -> m.Show:
    show = m.Show(
        name=f"Series {tmdb_id}",
        tmdb_id=tmdb_id,
        deleted_upstream_at=datetime.now(UTC) if deleted else None,
    )
    session.add(show)
    await session.flush()
    return show


async def _deleted_at(session, show_id: int) -> datetime | None:
    return (
        await session.execute(
            select(m.Show.deleted_upstream_at)
            .where(m.Show.id == show_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


# --- the diff ---------------------------------------------------------------


async def test_series_absent_from_the_export_is_tombstoned(session):
    live = await _add_show(session, tmdb_id=700)
    gone = await _add_show(session, tmdb_id=701)
    await session.commit()

    result = await reconcile_tombstones(session, feed_ids=_export(700))
    await session.commit()

    assert result == TombstoneResult(tombstoned=1, resurrected=0)
    assert await _deleted_at(session, live.id) is None
    assert await _deleted_at(session, gone.id) is not None


async def test_a_series_that_reappears_is_resurrected(session):
    show = await _add_show(session, tmdb_id=710, deleted=True)
    await session.commit()

    result = await reconcile_tombstones(session, feed_ids=_export(710))
    await session.commit()

    assert result == TombstoneResult(tombstoned=0, resurrected=1)
    assert await _deleted_at(session, show.id) is None


async def test_an_already_tombstoned_series_is_not_re_stamped(session):
    """The counter reports work done, so a steady state must report zero."""
    show = await _add_show(session, tmdb_id=715, deleted=True)
    await session.commit()
    before = await _deleted_at(session, show.id)

    result = await reconcile_tombstones(session, feed_ids=_export())
    await session.commit()

    assert result == TombstoneResult(tombstoned=0, resurrected=0)
    assert await _deleted_at(session, show.id) == before


async def test_the_diff_matches_on_tmdb_id_not_the_surrogate_key(session):
    """`catalog.show.id` means nothing to TMDB — the migration seeded it from
    TV Maze's ids (ADR-0008), so diffing on it would tombstone by coincidence."""
    show = await _add_show(session, tmdb_id=720)
    await session.commit()

    # The export names the series' TMDB id but not the row's surrogate id.
    assert show.id != 720
    result = await reconcile_tombstones(session, feed_ids=_export(720))
    await session.commit()

    assert result.tombstoned == 0
    assert await _deleted_at(session, show.id) is None


# --- locally-authored rows --------------------------------------------------


async def test_a_locally_authored_row_is_never_tombstoned(session):
    """The acceptance criterion. A row with no `tmdb_id` was never in the export,
    so a naive `mirrored - feed` would flag every one — including the TV Maze
    specials NEU-1042 copied in to hold watch history TMDB cannot supply."""
    local = await _add_show(session, tmdb_id=None)
    mapped = await _add_show(session, tmdb_id=730)
    await session.commit()

    result = await reconcile_tombstones(session, feed_ids=_export())
    await session.commit()

    assert result.tombstoned == 1, "only the mapped row is accountable to the export"
    assert await _deleted_at(session, local.id) is None
    assert await _deleted_at(session, mapped.id) is not None


# --- the floor guards -------------------------------------------------------


@pytest.mark.parametrize(
    "feed,label",
    [
        (set(), "empty"),
        (set(range(1, 10)), "tiny"),
        (set(range(1, _MIN_FEED_ABSOLUTE)), "one short of the floor"),
    ],
)
async def test_an_export_under_the_absolute_floor_writes_nothing(session, feed, label, caplog):
    """Trap: a truncated download would otherwise tombstone the whole catalog."""
    caplog.set_level("ERROR", logger="tvbf.tmdb.tombstone")
    show = await _add_show(session, tmdb_id=740)
    await session.commit()

    result = await reconcile_tombstones(session, feed_ids=feed)
    await session.commit()

    assert result.tombstoned == 0
    assert result.resurrected == 0
    assert result.skipped_reason is not None
    assert "absolute floor" in result.skipped_reason
    assert await _deleted_at(session, show.id) is None, f"{label} export must not tombstone"
    # The skip must be loud — it is the only signal that the guard fired.
    assert any(r.levelname == "ERROR" and "wrote nothing" in r.message for r in caplog.records), (
        "a skipped tombstone pass must log an error"
    )


async def test_an_export_that_clears_the_absolute_floor_can_still_be_refused(session, caplog):
    """The migration-window hole the relative floor's denominator closes: with a
    mirror of two rows, `95% of mirrored` would wave through an export missing a
    tenth of TMDB — and tombstone every mapped series in that tenth."""
    caplog.set_level("ERROR", logger="tvbf.tmdb.tombstone")
    show = await _add_show(session, tmdb_id=745)
    await session.commit()

    short = set(range(500_000, 500_000 + int(_MEASURED_EXPORT * 0.9)))
    assert len(short) > _MIN_FEED_ABSOLUTE, "the absolute floor must not be what catches this"

    result = await reconcile_tombstones(session, feed_ids=short)
    await session.commit()

    assert result.skipped_reason is not None
    assert "known" in result.skipped_reason
    assert await _deleted_at(session, show.id) is None


async def test_an_untrusted_export_does_not_resurrect_either(session):
    """An export we won't trust to prove absence can't be trusted to prove presence."""
    show = await _add_show(session, tmdb_id=750, deleted=True)
    await session.commit()

    result = await reconcile_tombstones(session, feed_ids={750})
    await session.commit()

    assert result.resurrected == 0
    assert result.skipped_reason is not None
    assert await _deleted_at(session, show.id) is not None


# --- what tombstoning must never do -----------------------------------------


async def test_tombstoning_never_deletes_a_row(session):
    """The test that fails if anyone reintroduces a DELETE.

    `app.user_show_watch` and `app.user_show_rating` cascade from the show row
    once the spine is repointed, so a delete here destroys user data nothing
    upstream could restore.
    """
    show = await _add_show(session, tmdb_id=760)
    season = m.Season(show_id=show.id, tmdb_id=7600, season_number=1)
    session.add(season)
    await session.flush()
    session.add(
        m.Episode(
            show_id=show.id,
            season_id=season.id,
            tmdb_id=76000,
            season_number=1,
            episode_number=1,
        )
    )
    await session.commit()

    before = [
        (await session.execute(select(func.count()).select_from(model))).scalar_one()
        for model in (m.Show, m.Season, m.Episode)
    ]

    await reconcile_tombstones(session, feed_ids=_export())
    await session.commit()

    after = [
        (await session.execute(select(func.count()).select_from(model))).scalar_one()
        for model in (m.Show, m.Season, m.Episode)
    ]
    assert before == after, "tombstoning must not remove show, season or episode rows"
    assert await _deleted_at(session, show.id) is not None


# --- riding along with the daily delta --------------------------------------

_YESTERDAY = (str(TODAY - timedelta(days=1)), str(TODAY))


@respx.mock
async def test_the_daily_delta_reconciles_against_the_export(session):
    """`/tv/changes` cannot report a deletion, so this pass is the only one that
    can — and the delta is where it runs (ADR-0005's cadence)."""
    await _seed_cursor(session, TODAY - timedelta(days=1))
    mock_changes({_YESTERDAY: [[1396]]})
    mock_series(1396, [1])
    stale = await _add_show(session, tmdb_id=999_001)
    await session.commit()

    await _run(session, export_ids=sorted(_export(1396)))

    assert await _deleted_at(session, stale.id) is not None, "absent from the export means gone"
    changed = (
        await session.execute(
            select(m.Show).where(m.Show.tmdb_id == 1396).execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert await _deleted_at(session, changed.id) is None


@respx.mock
async def test_an_aborted_delta_does_not_tombstone(session):
    """A run that gave up partway never saw the catalog it would be judging."""
    await _seed_cursor(session, TODAY - timedelta(days=1))
    mock_changes({_YESTERDAY: [[1396]]})
    respx.get(f"{BASE}/tv/1396").mock(return_value=httpx.Response(500))
    show = await _add_show(session, tmdb_id=999_002)
    await session.commit()

    _, result = await _run(session, export_ids=sorted(_export()), failure_threshold=1)

    assert result.aborted
    assert await _deleted_at(session, show.id) is None


@respx.mock
async def test_an_export_download_failure_does_not_fail_the_delta(session, caplog):
    """Best-effort by design: holding the cursor back over a failed second
    download would re-cover the whole window every night, widening forever."""
    caplog.set_level("ERROR", logger="tvbf.tmdb.update")
    await _seed_cursor(session, TODAY - timedelta(days=1))
    mock_changes({})
    respx.get(url__regex=r"https://files\.tmdb\.org/.*").mock(return_value=httpx.Response(503))

    run_id, result = await _run(session, export_ids=None)

    assert not result.aborted
    row = await _run_row(session, run_id)
    assert row.status == "succeeded", "the delta's own work stands"
    assert row.last_update_cursor is not None, "and the cursor still advances"
    assert any("tombstone pass failed" in r.message for r in caplog.records)
