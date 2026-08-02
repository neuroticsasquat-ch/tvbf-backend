from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from tvbf.tvmaze import models as m
from tvbf.tvmaze.api_payloads import TVMazeCastEntry, TVMazeCrewEntry
from tvbf.tvmaze.upsert import (
    mark_credits_synced,
    resolve_crew_role,
    upsert_show_cast,
    upsert_show_crew,
)


def cast_entry(person_id: int, character_id: int, name: str = "P") -> TVMazeCastEntry:
    return TVMazeCastEntry.model_validate(
        {
            "person": {"id": person_id, "name": name, "updated": 1},
            "character": {"id": character_id, "name": f"C{character_id}"},
            "self": False,
            "voice": False,
        }
    )


def crew_entry(person_id: int, type_: str, name: str = "P") -> TVMazeCrewEntry:
    return TVMazeCrewEntry.model_validate(
        {"type": type_, "person": {"id": person_id, "name": name, "updated": 1}}
    )


@pytest.fixture
async def a_show(session):
    session.add(m.Show(id=1, name="S", tvmaze_updated=1))
    await session.commit()


async def test_cast_insert_preserves_upstream_order(session, a_show):
    entries = [cast_entry(10, 100), cast_entry(11, 101), cast_entry(12, 102)]
    await upsert_show_cast(session, show_id=1, entries=entries)
    await session.commit()

    rows = (
        (
            await session.execute(
                select(m.ShowCast.person_id)
                .where(m.ShowCast.show_id == 1)
                .order_by(m.ShowCast.sort_order)
            )
        )
        .scalars()
        .all()
    )
    assert rows == [10, 11, 12]  # billing order, not id order


async def test_cast_populates_person_and_character_rows(session, a_show):
    entry = TVMazeCastEntry.model_validate(
        {
            "person": {
                "id": 30856,
                "name": "Zachary Levi",
                "country": {"name": "United States", "code": "US", "timezone": "America/New_York"},
                "birthday": "1980-09-29",
                "deathday": "",
                "gender": "Male",
                "image": {"medium": "https://m.jpg", "original": "https://o.jpg"},
                "updated": 1774528332,
            },
            "character": {
                "id": 45090,
                "name": 'Charles "Chuck" Bartowski',
                "image": {"medium": "https://cm.jpg", "original": None},
            },
            "self": True,
            "voice": True,
        }
    )
    await upsert_show_cast(session, show_id=1, entries=[entry])
    await session.commit()

    person = (await session.execute(select(m.Person).where(m.Person.id == 30856))).scalar_one()
    assert person.name == "Zachary Levi"
    assert person.country_code == "US"
    assert person.country_name == "United States"
    assert person.timezone == "America/New_York"
    assert person.birthday is not None and person.birthday.isoformat() == "1980-09-29"
    assert person.deathday is None
    assert person.gender == "Male"
    assert person.image_medium == "https://m.jpg"
    assert person.image_original == "https://o.jpg"
    assert person.tvmaze_updated == 1774528332
    # Pass C owns person credits; the show axis must not claim they are synced.
    assert person.credits_synced_at is None

    character = (
        await session.execute(select(m.Character).where(m.Character.id == 45090))
    ).scalar_one()
    assert character.name == 'Charles "Chuck" Bartowski'
    assert character.image_medium == "https://cm.jpg"
    assert character.image_original is None

    credit = (await session.execute(select(m.ShowCast).where(m.ShowCast.show_id == 1))).scalar_one()
    assert credit.is_self is True and credit.is_voice is True


async def test_reupsert_removes_departed_member(session, a_show):
    await upsert_show_cast(session, show_id=1, entries=[cast_entry(10, 100), cast_entry(11, 101)])
    await session.commit()
    await upsert_show_cast(session, show_id=1, entries=[cast_entry(11, 101)])
    await session.commit()

    rows = (
        (await session.execute(select(m.ShowCast.person_id).where(m.ShowCast.show_id == 1)))
        .scalars()
        .all()
    )
    assert rows == [11]


async def test_reupsert_refreshes_person_attributes(session, a_show):
    await upsert_show_cast(session, show_id=1, entries=[cast_entry(10, 100, name="Old")])
    await session.commit()
    await upsert_show_cast(session, show_id=1, entries=[cast_entry(10, 100, name="New")])
    await session.commit()

    person = (
        await session.execute(
            select(m.Person).where(m.Person.id == 10).execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert person.name == "New"


async def test_empty_cast_clears_existing_rows(session, a_show):
    await upsert_show_cast(session, show_id=1, entries=[cast_entry(10, 100)])
    await session.commit()
    await upsert_show_cast(session, show_id=1, entries=[])
    await session.commit()

    rows = (
        (await session.execute(select(m.ShowCast).where(m.ShowCast.show_id == 1))).scalars().all()
    )
    assert rows == []


async def test_cast_upsert_is_scoped_to_one_show(session, a_show):
    session.add(m.Show(id=2, name="Other", tvmaze_updated=1))
    await session.commit()
    await upsert_show_cast(session, show_id=1, entries=[cast_entry(10, 100)])
    await upsert_show_cast(session, show_id=2, entries=[cast_entry(11, 101)])
    await session.commit()

    await upsert_show_cast(session, show_id=1, entries=[])
    await session.commit()

    rows = (
        (await session.execute(select(m.ShowCast.person_id).where(m.ShowCast.show_id == 2)))
        .scalars()
        .all()
    )
    assert rows == [11]


async def test_duplicate_upstream_entries_are_deduped(session, a_show):
    # Upstream sending the same credit twice is one credit, not two rows.
    await upsert_show_cast(session, show_id=1, entries=[cast_entry(10, 100), cast_entry(10, 100)])
    await session.commit()
    rows = (
        (await session.execute(select(m.ShowCast).where(m.ShowCast.show_id == 1))).scalars().all()
    )
    assert len(rows) == 1


async def test_same_character_two_people_both_kept(session, a_show):
    # The Simpsons: Hank Azaria AND Harry Shearer both credited as Carl Carlson.
    await upsert_show_cast(session, show_id=1, entries=[cast_entry(10, 100), cast_entry(11, 100)])
    await session.commit()
    rows = (
        (
            await session.execute(
                select(m.ShowCast.person_id)
                .where(m.ShowCast.show_id == 1)
                .order_by(m.ShowCast.sort_order)
            )
        )
        .scalars()
        .all()
    )
    assert rows == [10, 11]


async def test_one_person_two_characters_both_kept(session, a_show):
    # Doubling up is common: one actor, two credited roles on the same show.
    await upsert_show_cast(session, show_id=1, entries=[cast_entry(10, 100), cast_entry(10, 101)])
    await session.commit()
    rows = (
        (
            await session.execute(
                select(m.ShowCast.character_id)
                .where(m.ShowCast.show_id == 1)
                .order_by(m.ShowCast.sort_order)
            )
        )
        .scalars()
        .all()
    )
    assert rows == [100, 101]


async def test_large_cast_exceeds_batch_size(session, a_show):
    # The Simpsons has 1,420 cast rows. Guard the bind-parameter ceiling.
    entries = [cast_entry(1000 + i, 5000 + i) for i in range(1200)]
    await upsert_show_cast(session, show_id=1, entries=entries)
    await session.commit()
    rows = (
        (await session.execute(select(m.ShowCast).where(m.ShowCast.show_id == 1))).scalars().all()
    )
    assert len(rows) == 1200
    ordered = (
        (
            await session.execute(
                select(m.ShowCast.person_id)
                .where(m.ShowCast.show_id == 1)
                .order_by(m.ShowCast.sort_order)
            )
        )
        .scalars()
        .all()
    )
    assert ordered == [1000 + i for i in range(1200)]


async def test_crew_insert_preserves_order_and_resolves_roles(session, a_show):
    entries = [
        crew_entry(20, "Executive Producer"),
        crew_entry(21, "Editor"),
        crew_entry(22, "Executive Producer"),
    ]
    await upsert_show_crew(session, show_id=1, entries=entries)
    await session.commit()

    rows = (
        (
            await session.execute(
                select(m.ShowCrew).where(m.ShowCrew.show_id == 1).order_by(m.ShowCrew.sort_order)
            )
        )
        .scalars()
        .all()
    )
    assert [r.person_id for r in rows] == [20, 21, 22]
    roles = dict((await session.execute(select(m.CrewRole.id, m.CrewRole.name))).all())
    assert sorted(roles.values()) == ["Editor", "Executive Producer"]
    assert roles[rows[0].role_id] == "Executive Producer"
    assert roles[rows[1].role_id] == "Editor"
    assert roles[rows[2].role_id] == "Executive Producer"


async def test_crew_role_resolve_is_idempotent(session, a_show):
    e = crew_entry(20, "Editor", name="E")
    await upsert_show_crew(session, show_id=1, entries=[e])
    await session.commit()
    await upsert_show_crew(session, show_id=1, entries=[e])
    await session.commit()

    roles = (await session.execute(select(m.CrewRole))).scalars().all()
    assert len(roles) == 1
    rows = (
        (await session.execute(select(m.ShowCrew).where(m.ShowCrew.show_id == 1))).scalars().all()
    )
    assert len(rows) == 1


async def test_resolve_crew_role_returns_same_id_across_calls(session):
    first = await resolve_crew_role(session, "Director")
    await session.commit()
    second = await resolve_crew_role(session, "Director")
    await session.commit()
    assert first == second


async def test_duplicate_crew_entries_are_deduped(session, a_show):
    await upsert_show_crew(
        session, show_id=1, entries=[crew_entry(20, "Editor"), crew_entry(20, "Editor")]
    )
    await session.commit()
    rows = (
        (await session.execute(select(m.ShowCrew).where(m.ShowCrew.show_id == 1))).scalars().all()
    )
    assert len(rows) == 1


async def test_one_person_two_crew_roles_both_kept(session, a_show):
    await upsert_show_crew(
        session, show_id=1, entries=[crew_entry(20, "Editor"), crew_entry(20, "Director")]
    )
    await session.commit()
    rows = (
        (await session.execute(select(m.ShowCrew).where(m.ShowCrew.show_id == 1))).scalars().all()
    )
    assert len(rows) == 2


async def test_empty_crew_clears_existing_rows(session, a_show):
    await upsert_show_crew(session, show_id=1, entries=[crew_entry(20, "Editor")])
    await session.commit()
    await upsert_show_crew(session, show_id=1, entries=[])
    await session.commit()

    rows = (
        (await session.execute(select(m.ShowCrew).where(m.ShowCrew.show_id == 1))).scalars().all()
    )
    assert rows == []


async def test_large_crew_exceeds_batch_size(session, a_show):
    # The Simpsons has 533 crew rows; the ceiling is the same one episodes hit.
    entries = [crew_entry(2000 + i, "Editor") for i in range(1200)]
    await upsert_show_crew(session, show_id=1, entries=entries)
    await session.commit()
    rows = (
        (await session.execute(select(m.ShowCrew).where(m.ShowCrew.show_id == 1))).scalars().all()
    )
    assert len(rows) == 1200


async def test_mark_credits_synced_sets_timestamp(session, a_show):
    before = datetime.now(UTC)
    await mark_credits_synced(session, show_id=1)
    await session.commit()

    refreshed = (
        await session.execute(
            select(m.Show).where(m.Show.id == 1).execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert refreshed.credits_synced_at is not None
    assert refreshed.credits_synced_at >= before
