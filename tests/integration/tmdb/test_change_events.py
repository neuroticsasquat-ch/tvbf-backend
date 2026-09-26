"""Catalog change detection through the delta's per-show path (NEU-1481, spec §5.1).

Each test mirrors a series twice through `mirror_series`: once as the full pass
would, to put the "before" rows in place, then again under the run kind being
tested with the payload changed. The comparison rules themselves are unit-tested
in `tests/unit/tmdb/test_change_events.py`; what is asserted here is the read
and write around them — that the snapshot is taken before the upsert, that the
event lands with the run and the right season, and that the gates hold.
"""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from sqlalchemy import select, update

from tests.fixtures.tmdb.series_factory import make_series
from tvbf.app import models as am
from tvbf.catalog import models as m
from tvbf.catalog.runs import create_run
from tvbf.tmdb.client import TMDBClient
from tvbf.tmdb.ingest import mirror_series

BASE = "https://api.themoviedb.org/3"
TMDB_ID = 1396

TODAY = datetime.now(UTC).date()
PAST = (TODAY - timedelta(days=300)).isoformat()
FUTURE = (TODAY + timedelta(days=60)).isoformat()
LATER = (TODAY + timedelta(days=75)).isoformat()


def _payload(status: str, dates: dict[int, str]) -> dict:
    """A series whose seasons `1..N` carry `dates`, every season block appended."""
    payload = make_series(TMDB_ID, seasons=len(dates), status=status)
    for summary in payload["seasons"]:
        summary["air_date"] = dates[summary["season_number"]]
    return payload


async def _mirror(session, kind: str, payload: dict):
    run_id = await create_run(session, kind=kind)
    await session.commit()
    with respx.mock:
        respx.get(f"{BASE}/tv/{TMDB_ID}").mock(return_value=httpx.Response(200, json=payload))
        async with TMDBClient(
            base_url=BASE,
            read_access_token="eyJ-not-a-real-token",
            rate_calls=200,
            rate_window=1,
            retry_base_delay=0.01,
        ) as client:
            result = await mirror_series(
                session_factory=lambda: session,
                client=client,
                run_id=run_id,
                series_ids=[TMDB_ID],
            )
    assert result.shows_processed == 1
    return run_id


async def _track(session, make_user) -> int:
    user = await make_user()
    show_id = await session.scalar(select(m.Show.id).where(m.Show.tmdb_id == TMDB_ID))
    session.add(am.UserShowWatch(user_id=user.id, show_id=show_id))
    await session.commit()
    return show_id


async def _events(session) -> list[m.ShowEvent]:
    return list(
        (await session.execute(select(m.ShowEvent).order_by(m.ShowEvent.id))).scalars().all()
    )


async def _season_id(session, show_id: int, number: int) -> int:
    return await session.scalar(
        select(m.Season.id).where(m.Season.show_id == show_id, m.Season.season_number == number)
    )


async def test_a_new_future_season_is_premiere_set(session, make_user):
    await _mirror(session, "catalog_initial", _payload("Returning Series", {1: PAST}))
    show_id = await _track(session, make_user)

    run_id = await _mirror(
        session, "catalog_update", _payload("Returning Series", {1: PAST, 2: FUTURE})
    )

    [event] = await _events(session)
    assert (event.kind, event.show_id, event.old_value, event.new_value, event.run_id) == (
        "premiere_set",
        show_id,
        None,
        FUTURE,
        run_id,
    )
    assert event.season_id == await _season_id(session, show_id, 2)


async def test_a_future_premiere_changing_date_is_premiere_moved(session, make_user):
    await _mirror(session, "catalog_initial", _payload("Returning Series", {1: FUTURE}))
    show_id = await _track(session, make_user)

    run_id = await _mirror(session, "catalog_update", _payload("Returning Series", {1: LATER}))

    [event] = await _events(session)
    assert (event.kind, event.old_value, event.new_value, event.run_id) == (
        "premiere_moved",
        FUTURE,
        LATER,
        run_id,
    )
    assert event.season_id == await _season_id(session, show_id, 1)


async def test_a_show_entering_ended_is_ended(session, make_user):
    await _mirror(session, "catalog_initial", _payload("Returning Series", {1: PAST}))
    show_id = await _track(session, make_user)

    run_id = await _mirror(session, "catalog_update", _payload("Canceled", {1: PAST}))

    [event] = await _events(session)
    assert (
        event.kind,
        event.show_id,
        event.season_id,
        event.old_value,
        event.new_value,
        event.run_id,
    ) == ("ended", show_id, None, "Returning Series", "Canceled", run_id)


async def test_a_show_leaving_ended_is_revived(session, make_user):
    await _mirror(session, "catalog_initial", _payload("Ended", {1: PAST}))
    await _track(session, make_user)

    run_id = await _mirror(session, "catalog_update", _payload("Returning Series", {1: PAST}))

    [event] = await _events(session)
    assert (event.kind, event.old_value, event.new_value, event.run_id) == (
        "revived",
        "Ended",
        "Returning Series",
        run_id,
    )


async def test_an_untracked_show_records_nothing(session):
    await _mirror(session, "catalog_initial", _payload("Returning Series", {1: PAST}))

    await _mirror(session, "catalog_update", _payload("Ended", {1: PAST, 2: FUTURE}))

    assert await _events(session) == []


@pytest.mark.parametrize("kind", ["catalog_initial", "episode_credits_backfill"])
async def test_only_the_delta_records_anything(session, make_user, kind):
    await _mirror(session, "catalog_initial", _payload("Returning Series", {1: PAST}))
    await _track(session, make_user)

    await _mirror(session, kind, _payload("Ended", {1: PAST, 2: FUTURE}))

    assert await _events(session) == []


async def test_an_airdate_offset_is_not_a_move(session, make_user):
    """The stored `air_date` is a day later than TMDB's once the airdate pass
    corrects it; the comparison must read the raw `tmdb_air_date` beside it."""
    await _mirror(session, "catalog_initial", _payload("Returning Series", {1: FUTURE}))
    show_id = await _track(session, make_user)
    raw = datetime.fromisoformat(FUTURE).date()
    await session.execute(
        update(m.Season)
        .where(m.Season.show_id == show_id)
        .values(air_date=raw + timedelta(days=1), tmdb_air_date=raw)
    )
    await session.commit()

    await _mirror(session, "catalog_update", _payload("Returning Series", {1: FUTURE}))

    assert await _events(session) == []
