from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from tvbf.catalog import models as m
from tvbf.catalog.events import recent_events, record_events
from tvbf.catalog.runs import create_run

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


async def _show_with_season(session, show_id: int = 1, season_id: int = 10) -> None:
    session.add(m.Show(id=show_id, tmdb_id=show_id, name=f"Show {show_id}"))
    await session.flush()
    session.add(m.Season(id=season_id, tmdb_id=season_id, show_id=show_id, season_number=1))
    await session.flush()


async def _pin_observed_at(session, event_id: int, at: datetime) -> None:
    await session.execute(
        update(m.ShowEvent).where(m.ShowEvent.id == event_id).values(observed_at=at)
    )


async def test_record_events_writes_every_row_in_the_callers_transaction(session):
    await _show_with_season(session)
    run_id = await create_run(session, kind="catalog_update")
    events = [
        m.ShowEvent(
            kind="premiere_set",
            show_id=1,
            season_id=10,
            old_value=None,
            new_value="2026-10-15",
            run_id=run_id,
        ),
        m.ShowEvent(kind="ended", show_id=1, old_value="Returning Series", new_value="Ended"),
    ]

    await record_events(session, events)

    rows = (await session.execute(select(m.ShowEvent).order_by(m.ShowEvent.id))).scalars().all()
    assert [(r.kind, r.season_id, r.new_value, r.run_id) for r in rows] == [
        ("premiere_set", 10, "2026-10-15", run_id),
        ("ended", None, "Ended", None),
    ]
    assert all(r.id is not None and r.observed_at is not None for r in rows)


async def test_record_events_with_nothing_to_record_is_a_no_op(session):
    await record_events(session, [])

    assert (await session.execute(select(m.ShowEvent))).first() is None


async def test_recent_events_filters_on_kind_and_window_oldest_first(session):
    await _show_with_season(session)
    await record_events(
        session,
        [
            m.ShowEvent(kind="premiere_moved", show_id=1, season_id=10),  # too old
            m.ShowEvent(kind="ended", show_id=1),  # newest
            m.ShowEvent(kind="revived", show_id=1),  # kind not asked for
            m.ShowEvent(kind="premiere_set", show_id=1, season_id=10),  # exactly on `since`
        ],
    )
    too_old, newest, other_kind, on_boundary = (
        (await session.execute(select(m.ShowEvent.id).order_by(m.ShowEvent.id))).scalars().all()
    )
    since = NOW - timedelta(hours=48)
    await _pin_observed_at(session, too_old, since - timedelta(seconds=1))
    await _pin_observed_at(session, newest, NOW)
    await _pin_observed_at(session, other_kind, NOW)
    await _pin_observed_at(session, on_boundary, since)

    found = await recent_events(
        session, since=since, kinds=("premiere_set", "premiere_moved", "ended")
    )

    assert [e.id for e in found] == [on_boundary, newest]


async def test_recent_events_breaks_observed_at_ties_by_id(session):
    await _show_with_season(session)
    await record_events(
        session, [m.ShowEvent(kind="ended", show_id=1), m.ShowEvent(kind="revived", show_id=1)]
    )

    found = await recent_events(
        session, since=NOW - timedelta(days=365), kinds=("ended", "revived")
    )

    # One transaction, one `now()`: the two share `observed_at`.
    assert found[0].observed_at == found[1].observed_at
    assert found[0].id < found[1].id


async def test_recent_events_with_no_kinds_returns_nothing(session):
    await _show_with_season(session)
    await record_events(session, [m.ShowEvent(kind="ended", show_id=1)])

    assert await recent_events(session, since=NOW - timedelta(days=365), kinds=()) == []


async def test_show_event_kind_is_constrained(session):
    await _show_with_season(session)
    session.add(m.ShowEvent(kind="episode_added", show_id=1))

    with pytest.raises(IntegrityError, match="ck_show_event_kind"):
        await session.flush()


async def test_pruning_a_season_takes_its_events_with_it(session):
    await _show_with_season(session)
    await record_events(
        session,
        [
            m.ShowEvent(kind="premiere_set", show_id=1, season_id=10),
            m.ShowEvent(kind="ended", show_id=1),
        ],
    )

    await session.execute(delete(m.Season).where(m.Season.id == 10))

    kinds = (await session.execute(select(m.ShowEvent.kind))).scalars().all()
    assert kinds == ["ended"]


async def test_push_deliver_is_an_accepted_run_kind(session):
    run_id = await create_run(session, kind="push_deliver")

    row = (await session.execute(select(m.IngestRun).where(m.IngestRun.id == run_id))).scalar_one()
    assert row.kind == "push_deliver"
