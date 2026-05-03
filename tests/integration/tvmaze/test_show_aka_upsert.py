from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from tvbf.tvmaze import models as m
from tvbf.tvmaze.schemas import TVMazeAka
from tvbf.tvmaze.upsert import mark_akas_synced, upsert_akas


@pytest.fixture
async def show_in_db(session):
    s = m.Show(
        id=4242,
        name="東京リベンジャーズ",
        tvmaze_updated=1700000000,
    )
    session.add(s)
    await session.commit()
    return s


async def test_upsert_akas_inserts_rows(session, show_in_db):
    akas = [
        TVMazeAka.model_validate(
            {
                "name": "Tokyo Revengers",
                "country": {"code": "US", "name": "United States"},
                "language": "en",
            }
        ),
        TVMazeAka.model_validate({"name": "Tokyo卍Revengers", "country": None, "language": None}),
    ]
    await upsert_akas(session, show_id=4242, akas=akas)
    await session.commit()

    rows = (
        (await session.execute(select(m.ShowAka).where(m.ShowAka.show_id == 4242))).scalars().all()
    )
    assert len(rows) == 2
    by_name = {r.name: r for r in rows}
    assert by_name["Tokyo Revengers"].country_code == "US"
    assert by_name["Tokyo Revengers"].country_name == "United States"
    assert by_name["Tokyo Revengers"].language == "en"
    assert by_name["Tokyo卍Revengers"].country_code is None
    assert by_name["Tokyo卍Revengers"].country_name is None
    assert by_name["Tokyo卍Revengers"].language is None


async def test_upsert_akas_replaces_existing_rows(session, show_in_db):
    first = [TVMazeAka.model_validate({"name": "Old Title", "country": None})]
    await upsert_akas(session, show_id=4242, akas=first)
    await session.commit()

    second = [
        TVMazeAka.model_validate({"name": "New Title", "country": None}),
        TVMazeAka.model_validate({"name": "Another", "country": None}),
    ]
    await upsert_akas(session, show_id=4242, akas=second)
    await session.commit()

    rows = (
        (await session.execute(select(m.ShowAka.name).where(m.ShowAka.show_id == 4242)))
        .scalars()
        .all()
    )
    assert sorted(rows) == ["Another", "New Title"]


async def test_upsert_empty_clears_rows(session, show_in_db):
    await upsert_akas(
        session,
        show_id=4242,
        akas=[TVMazeAka.model_validate({"name": "X", "country": None})],
    )
    await session.commit()

    await upsert_akas(session, show_id=4242, akas=[])
    await session.commit()

    rows = (
        (await session.execute(select(m.ShowAka).where(m.ShowAka.show_id == 4242))).scalars().all()
    )
    assert rows == []


async def test_mark_akas_synced_sets_timestamp(session, show_in_db):
    before = datetime.now(UTC)
    await mark_akas_synced(session, show_id=4242)
    await session.commit()

    refreshed = (
        await session.execute(
            select(m.Show).where(m.Show.id == 4242).execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert refreshed.akas_synced_at is not None
    assert refreshed.akas_synced_at >= before
