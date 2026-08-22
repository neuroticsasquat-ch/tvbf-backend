"""NEU-1172 — `catalog.show.popularity` refreshed from the daily id export.

`/tv/changes` never reports a popularity move, so without this pass the column
freezes for the 97% of the catalog a delta does not re-fetch. What has to hold:
it writes that one column and no other, it advances neither sync watermark, it
matches on `tmdb_id`, and it refuses a short export exactly as the tombstone
pass does — the two read the same file and must not disagree about it.
"""

import gzip
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from sqlalchemy import select

from tests.integration.tmdb.test_ingest import mock_series
from tests.integration.tmdb.test_update import TODAY, _run, _seed_cursor, mock_changes
from tvbf.catalog import models as m
from tvbf.tmdb.export import _MEASURED_EXPORT, ExportEntry
from tvbf.tmdb.popularity import PopularityResult, refresh_popularity

# The floors permit only a realistic export, so tests need a realistic one. The
# filler ids sit well clear of the ones under test and carry no score, which is
# also what keeps the batching honest: `scored` counts what was offered.
_FILLER = [
    ExportEntry(tmdb_id=i, popularity=None) for i in range(500_000, 500_000 + _MEASURED_EXPORT)
]


def _export(*entries: ExportEntry) -> list[ExportEntry]:
    """A plausible export listing exactly the given series, plus filler."""
    return [*_FILLER, *entries]


async def _add_show(session, *, tmdb_id: int | None, popularity: float | None = None) -> m.Show:
    show = m.Show(name=f"Series {tmdb_id}", tmdb_id=tmdb_id, popularity=popularity)
    session.add(show)
    await session.flush()
    return show


async def _reread(session, show_id: int) -> m.Show:
    return (
        await session.execute(
            select(m.Show).where(m.Show.id == show_id).execution_options(populate_existing=True)
        )
    ).scalar_one()


# --- the write --------------------------------------------------------------


async def test_an_exported_score_lands_on_the_mirrored_row(session):
    show = await _add_show(session, tmdb_id=800, popularity=1.5)
    await session.commit()

    result = await refresh_popularity(
        session, entries=_export(ExportEntry(tmdb_id=800, popularity=91.25))
    )
    await session.commit()

    assert result == PopularityResult(updated=1, scored=1)
    assert (await _reread(session, show.id)).popularity == 91.25


async def test_the_match_is_on_tmdb_id_not_the_surrogate_key(session):
    """`catalog.show.id` is a surrogate the migration seeded from TV Maze's ids
    (ADR-0008) and means nothing to the export."""
    show = await _add_show(session, tmdb_id=801, popularity=1.0)
    await session.commit()
    assert show.id != 801, "the fixture must not accidentally line the two up"

    await refresh_popularity(
        session, entries=_export(ExportEntry(tmdb_id=show.id, popularity=77.0))
    )
    await session.commit()

    assert (await _reread(session, show.id)).popularity == 1.0


async def test_a_show_with_no_score_yet_gets_one(session):
    show = await _add_show(session, tmdb_id=802, popularity=None)
    await session.commit()

    await refresh_popularity(session, entries=_export(ExportEntry(tmdb_id=802, popularity=4.25)))
    await session.commit()

    assert (await _reread(session, show.id)).popularity == 4.25


# --- what it must not touch -------------------------------------------------


async def test_it_writes_popularity_and_nothing_else(session):
    """Not an ingest, and it must not become one — the export's `original_name`
    is not `catalog.show.name` and is no substitute for one."""
    show = m.Show(
        name="Kept",
        tmdb_id=810,
        popularity=1.0,
        overview="kept too",
        original_language="en",
        vote_count=7,
        status="Returning Series",
    )
    session.add(show)
    await session.commit()

    await refresh_popularity(session, entries=_export(ExportEntry(tmdb_id=810, popularity=50.0)))
    await session.commit()

    after = await _reread(session, show.id)
    assert (after.name, after.overview, after.original_language, after.vote_count) == (
        "Kept",
        "kept too",
        "en",
        7,
    )
    assert after.status == "Returning Series"
    assert after.popularity == 50.0


async def test_it_advances_neither_sync_watermark(session):
    """A score arriving from the export is not evidence a payload was mirrored.
    Stamping either watermark would retire the show from a work list it belongs
    on — the distinction NEU-1127 had to draw."""
    synced = datetime(2026, 8, 10, tzinfo=UTC)
    show = m.Show(name="Stale", tmdb_id=811, tmdb_synced_at=synced, credits_synced_at=None)
    session.add(show)
    await session.commit()

    await refresh_popularity(session, entries=_export(ExportEntry(tmdb_id=811, popularity=9.0)))
    await session.commit()

    after = await _reread(session, show.id)
    assert after.tmdb_synced_at == synced
    assert after.credits_synced_at is None


async def test_a_series_only_in_the_export_is_skipped_silently(session):
    """The ingest's work list answers a missing series; this pass does not."""
    result = await refresh_popularity(
        session, entries=_export(ExportEntry(tmdb_id=999_123, popularity=12.0))
    )
    await session.commit()

    assert result == PopularityResult(updated=0, scored=1)


async def test_a_series_only_in_the_mirror_keeps_its_score(session):
    """Absence from the export is the tombstone pass's question. Nulling the
    column here would destroy a score over a series TMDB merely stopped
    listing."""
    show = await _add_show(session, tmdb_id=812, popularity=33.0)
    locally_authored = await _add_show(session, tmdb_id=None, popularity=44.0)
    await session.commit()

    await refresh_popularity(session, entries=_export())
    await session.commit()

    assert (await _reread(session, show.id)).popularity == 33.0
    assert (await _reread(session, locally_authored.id)).popularity == 44.0


async def test_a_line_with_no_usable_popularity_leaves_the_row_alone(session):
    """Its id still counts as present for the tombstone diff; only the score is
    missing, and a missing score is not a score of null."""
    show = await _add_show(session, tmdb_id=813, popularity=21.0)
    await session.commit()

    result = await refresh_popularity(
        session, entries=_export(ExportEntry(tmdb_id=813, popularity=None))
    )
    await session.commit()

    assert result == PopularityResult(updated=0, scored=0)
    assert (await _reread(session, show.id)).popularity == 21.0


# --- idempotence ------------------------------------------------------------


async def test_re_running_against_unchanged_scores_writes_nothing(session):
    """`IS DISTINCT FROM` is what makes the nightly pass free rather than merely
    idempotent — without it every run rewrites 229k rows and their dead tuples."""
    await _add_show(session, tmdb_id=820, popularity=None)
    await session.commit()
    entries = _export(ExportEntry(tmdb_id=820, popularity=6.5))

    first = await refresh_popularity(session, entries=entries)
    await session.commit()
    second = await refresh_popularity(session, entries=entries)
    await session.commit()

    assert first.updated == 1
    assert second == PopularityResult(updated=0, scored=1)


async def test_the_write_batches_past_the_bind_parameter_cap(session):
    """229k rows against Postgres's 32,767 bind parameters is the ceiling that
    forces batching everywhere else in this package."""
    shows = [await _add_show(session, tmdb_id=830_000 + i) for i in range(3)]
    await session.commit()
    scores = [ExportEntry(tmdb_id=830_000 + i, popularity=float(i)) for i in range(3)]

    result = await refresh_popularity(session, entries=_export(*scores), batch_size=1)
    await session.commit()

    assert result == PopularityResult(updated=3, scored=3)
    assert [(await _reread(session, s.id)).popularity for s in shows] == [0.0, 1.0, 2.0]


# --- the floors -------------------------------------------------------------


@pytest.mark.parametrize(
    "entries, why",
    [
        ([], "an empty export"),
        ([ExportEntry(tmdb_id=1, popularity=1.0)], "a one-line export"),
    ],
)
async def test_a_short_export_writes_no_popularity(session, entries, why, caplog):
    """The same floors the tombstone pass consults, on the same download. A
    partial file that skewed scores here while writing no tombstones would leave
    the two disagreeing about what upstream holds."""
    caplog.set_level("ERROR", logger="tvbf.tmdb.popularity")
    show = await _add_show(session, tmdb_id=1, popularity=2.0)
    await session.commit()

    result = await refresh_popularity(session, entries=entries)
    await session.commit()

    assert result.updated == 0
    assert result.skipped_reason is not None, why
    assert (await _reread(session, show.id)).popularity == 2.0
    assert any("wrote nothing" in r.message for r in caplog.records)


async def test_a_tombstoned_row_does_not_count_toward_the_floor(session):
    """Counting it would make the guard self-wedging once the tombstoned
    population passed 5% of the mirror — the reason the tombstone pass counts
    live rows only."""
    await _add_show(session, tmdb_id=840)
    gone = m.Show(name="Gone", tmdb_id=841, deleted_upstream_at=datetime.now(UTC))
    session.add(gone)
    await session.commit()

    result = await refresh_popularity(
        session, entries=_export(ExportEntry(tmdb_id=840, popularity=3.0))
    )

    assert result.skipped_reason is None


# --- riding along with the daily delta --------------------------------------

_YESTERDAY = (str(TODAY - timedelta(days=1)), str(TODAY))


@respx.mock
async def test_the_daily_delta_refreshes_popularity(session):
    """It runs where the export is already in hand. A second download for a
    second job would double a 5 MB transfer for nothing."""
    await _seed_cursor(session, TODAY - timedelta(days=1))
    mock_changes({_YESTERDAY: [[1396]]})
    mock_series(1396, [1])
    untouched = await _add_show(session, tmdb_id=999_010, popularity=1.0)
    await session.commit()

    await _run(
        session,
        export_entries=_export(
            ExportEntry(tmdb_id=1396, popularity=5.0),
            ExportEntry(tmdb_id=999_010, popularity=88.5),
        ),
    )

    assert (await _reread(session, untouched.id)).popularity == 88.5, (
        "a show the delta never re-fetched is exactly what this pass is for"
    )


@respx.mock
async def test_a_truncated_download_writes_no_popularity(session, caplog):
    """AC 2, the second half: a short file writes no score for the same reason
    it writes no tombstone. The gzip trailer catches it in `parse_series_export`,
    ahead of both passes, so neither ever sees the partial catalog."""
    caplog.set_level("ERROR", logger="tvbf.tmdb.update")
    await _seed_cursor(session, TODAY - timedelta(days=1))
    mock_changes({})
    whole = gzip.compress(
        "\n".join(json.dumps({"id": i, "popularity": 99.0}) for i in range(1, 500)).encode()
    )
    respx.get(url__regex=r"https://files\.tmdb\.org/.*").mock(
        return_value=httpx.Response(200, content=whole[: len(whole) // 2])
    )
    show = await _add_show(session, tmdb_id=1, popularity=2.0)
    await session.commit()

    _, result = await _run(session, export_ids=None)

    assert not result.aborted, "the delta's own work still stands"
    assert (await _reread(session, show.id)).popularity == 2.0
    assert any("id export download failed" in r.message for r in caplog.records)
